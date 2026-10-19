"""Human account invitations delegated to exactly one registered application."""
from dataclasses import replace
from datetime import timedelta
import json
import re
import secrets

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (ApplicationAccountAccess, ApplicationAccountInvitation, AuditEvent,
    Invitation, Membership, Organization, User)


def roles_for(deployment):
    from platform_services.validation import validate_manifest
    validate_manifest(deployment.installation.manifest)
    return deployment.installation.manifest.get('account_roles', [])


def selected_roles(deployment, role_ids):
    roles = {role['id']: role for role in roles_for(deployment)}
    if (not isinstance(role_ids, list) or len(role_ids) != 1 or
            not isinstance(role_ids[0], str) or role_ids[0] not in roles):
        raise ValidationError('Choose one published application role.')
    return [roles[role_ids[0]]]


def account_for(membership, deployment):
    if not membership or membership.organization_id != deployment.installation.organization_id or not membership.active:
        return None
    from .policy_evaluation import policy_fact
    return policy_fact(('application_account', str(membership.pk), str(deployment.pk)),
        lambda: ApplicationAccountAccess.objects.filter(membership=membership, deployment=deployment, active=True).first())


def application_grants(membership, principal):
    """Only the current application can consume its explicit account grant."""
    if not principal or not principal.application or not membership:
        return set(), set()
    from platform_services.external_apps import application_deployment, external_available
    deployment = application_deployment(principal.application)
    if not deployment or not deployment.installation.manifest.get('account_roles') or not external_available(deployment):
        return set(), set()
    account = account_for(membership, deployment)
    if not account:
        return set(), set()
    try:
        roles = selected_roles(deployment, account.role_ids)
    except ValidationError:
        return set(), set()
    return {permission for role in roles for permission in role['permissions']}, set(account.role_ids)


def application_boundary_grants(membership, principal, deployment, chain, tags):
    """An application account cannot grant a human access to another resource/app."""
    if not deployment or not deployment.installation.manifest.get('account_roles'):
        return set()
    account = account_for(membership, deployment)
    if not account: return set()
    try: selected_roles(deployment, account.role_ids)
    except ValidationError: return set()
    from platform_services.external_apps import application_deployment
    current_deployment = application_deployment(principal.application) if principal.application else None
    if not current_deployment or current_deployment.pk != deployment.pk:
        return set()
    # Permit the application's own OAuth/service/permission chain only. It must
    # never turn into an organization-wide environment grant for data or admin APIs.
    allowed_types = {'identity.application', 'identity.service', 'identity.servicepermission',
        'identity.organization', 'platform_services.deployment', 'platform_services.installation',
        'platform_services.deploymentenvironment', 'platform_services.marketplacepackage'}
    if any(item._meta.label_lower not in allowed_types for item in chain):
        return set()
    for item in chain:
        label = item._meta.label_lower
        if label == 'identity.application' and item.pk not in {deployment.application_id, deployment.resource_application_id}: return set()
        if label == 'identity.service' and item.pk != deployment.installation.service_id: return set()
        if label == 'identity.servicepermission' and item.service_id != deployment.installation.service_id: return set()
        if label == 'platform_services.deployment' and item.pk != deployment.pk: return set()
        if label == 'platform_services.installation' and item.pk != deployment.installation_id: return set()
        if label == 'identity.organization' and item.pk != deployment.installation.organization_id: return set()
        if label == 'platform_services.deploymentenvironment' and item.pk != deployment.environment_id: return set()
        if label == 'platform_services.marketplacepackage' and item.pk != deployment.installation.package_id: return set()
    return {deployment.environment.boundary_tag_id} if deployment.environment_id else set()


def invitation_output(record):
    invitation = record.invitation
    status = 'revoked' if record.revoked_at else 'accepted' if invitation.accepted else 'expired' if invitation.expires <= timezone.now() else 'pending'
    return {'id': str(invitation.pk), 'email': invitation.email, 'name': record.name,
        'roleIds': record.role_ids, 'status': status, 'expiresAt': invitation.expires.isoformat()}


def audit(principal, deployment, action, target):
    AuditEvent.objects.create(organization=deployment.installation.organization, actor=principal.user,
        action=action, target=str(target), request_id=principal.request_id)


def accept_application_invitation(invitation, membership, application=None):
    record = ApplicationAccountInvitation.objects.select_related('deployment__installation__service',
        'deployment__installation__organization', 'deployment__installation__package', 'deployment__environment',
        'deployment__application', 'deployment__resource_application').filter(invitation=invitation).first()
    if not record: return None
    from platform_services.external_apps import application_deployment, external_available
    if (record.revoked_at or record.deployment.installation.organization_id != membership.organization_id or
            not external_available(record.deployment) or
            application and (not application_deployment(application) or application_deployment(application).pk != record.deployment_id)):
        raise PermissionDenied('This application invitation is no longer available.')
    selected_roles(record.deployment, record.role_ids)
    # Acceptance never changes an existing explicit application account, global
    # membership kind, organization roles, other apps or unrelated access tags.
    ApplicationAccountAccess.objects.get_or_create(membership=membership, deployment=record.deployment,
        defaults={'role_ids': record.role_ids})
    return record.deployment.endpoint


def run_operation(request):
    from .access import Principal, evidence_for
    from .views import authenticate_resource_client, token_claims
    from platform_services.external_apps import application_deployment
    resource = authenticate_resource_client(request)
    if not resource: return {'error': 'invalid_client'}, 401
    if set(request.POST) != {'token', 'operation', 'payload'}: raise ValidationError('Unsupported account request fields.')
    raw = request.POST.get('token', '')
    if not 1 <= len(raw) <= 4096: return {'error': 'invalid_token'}, 401
    claims = token_claims(raw, resource, request_id=getattr(request, 'security_request_id', None))
    if not claims: return {'error': 'invalid_token'}, 401
    if not {'admin', 'accounts-manage'} & set(claims['permissions']): raise PermissionDenied('Account administration is not granted.')
    deployment = application_deployment(resource)
    if not deployment or deployment.target != 'external' or deployment.resource_application_id != resource.pk:
        raise PermissionDenied('Choose the registered application resource client.')
    if not roles_for(deployment): raise PermissionDenied('This application has not published account roles.')
    principal = Principal(user=User.objects.get(pk=claims['sub']), evidence=evidence_for(raw),
        application=deployment.application, request_id=getattr(request, 'security_request_id', None))
    if len(request.POST['payload']) > 8192: raise ValidationError('Account request is too large.')
    try: body = json.loads(request.POST['payload'])
    except ValueError: raise ValidationError('Use a JSON account request.')
    if not isinstance(body, dict): raise ValidationError('Use an account request object.')
    envelope = {'organization_id': claims['organization_id'], 'service_id': claims['service_id']}
    operation = request.POST['operation']
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=resource.service.organization_id)
        current_claims = token_claims(raw, resource, request_id=principal.request_id)
        if not current_claims or not {'admin', 'accounts-manage'} & set(current_claims['permissions']):
            raise PermissionDenied('Account administration is no longer granted.')
        claims = current_claims
        # The locked token proof above binds the human to this exact app. The
        # following queries separately bind each roster/invitation target to it.
        # Identity metadata has no implicit deployment placement; remove only
        # that injected context, retaining all explicit/inherited target and
        # organization tags, authentication evidence and purpose restrictions.
        identity_principal = replace(principal, application=None)
        if operation == 'list':
            if body: raise ValidationError('Account listing does not accept filters.')
            members = Membership.objects.filter(organization=resource.service.organization).filter(
                Q(application_accounts__deployment=deployment) | Q(kind='administrator') |
                Q(roles__permissions__service=resource.service)).select_related('user').distinct().order_by('user__email')[:201]
            from .access import decision
            accounts = []
            for member in members:
                if not decision(identity_principal, member)[0]: continue
                access = ApplicationAccountAccess.objects.filter(membership=member, deployment=deployment).first()
                role_ids = access.role_ids if access else []
                if not access:
                    permissions = set(resource.service.permissions.filter(roles__memberships=member).values_list('codename', flat=True))
                    if member.kind == 'administrator': role_ids = ['administrator']
                    else:
                        matches = [role for role in roles_for(deployment) if set(role['permissions']) <= permissions]
                        role_ids = [max(matches, key=lambda role: len(role['permissions']))['id']] if matches else []
                accounts.append({'id': str(member.user_id), 'name': member.user.get_full_name(), 'email': member.user.email,
                    'status': 'active' if member.active and member.user.is_active and (not access or access.active) else 'inactive', 'roleIds': role_ids})
            if len(accounts) > 200: raise ValidationError('Use Core organization administration for directories above 200 accounts.')
            invitations = [invitation_output(record) for record in ApplicationAccountInvitation.objects.filter(deployment=deployment)
                .select_related('invitation').order_by('-pk')[:200] if decision(identity_principal, record.invitation)[0]]
            audit(principal, deployment, 'application.accounts_listed', deployment.pk)
            return {**envelope, 'accounts': accounts, 'invitations': invitations,
                'roles': [{key: role[key] for key in ('id', 'name', 'description')} for role in roles_for(deployment)]}, 200
        if operation == 'invite':
            if set(body) != {'email', 'name', 'roleIds'}: raise ValidationError('Supply an email, name, and application role.')
            email, name = body.get('email'), body.get('name')
            if not isinstance(email, str) or len(email) > 254: raise ValidationError('Enter a valid email.')
            email = email.strip().lower(); validate_email(email)
            if not isinstance(name, str) or len(name) > 160 or any(ord(char) < 32 for char in name): raise ValidationError('Enter an account name of at most 160 characters.')
            selected = selected_roles(deployment, body.get('roleIds'))
            if any(set(role['permissions']) - set(claims['permissions']) for role in selected):
                raise PermissionDenied('You cannot delegate permissions you do not hold.')
            if ApplicationAccountInvitation.objects.filter(deployment=deployment, revoked_at__isnull=True,
                    invitation__email__iexact=email, invitation__accepted=False, invitation__expires__gt=timezone.now()).exists():
                return {'error': 'invitation_pending'}, 409
            existing_membership = Membership.objects.filter(organization=resource.service.organization,
                user__email__iexact=email).first()
            if existing_membership and (ApplicationAccountAccess.objects.filter(membership=existing_membership, deployment=deployment).exists() or
                    existing_membership.kind == 'administrator' or existing_membership.roles.filter(permissions__service=resource.service).exists()):
                return {'error': 'application_account_exists'}, 409
            secret = secrets.token_urlsafe(32)
            invitation = Invitation.objects.create(organization=resource.service.organization, email=email,
                kind=Membership.Kind.MEMBER, token_hash=Invitation.digest(secret), expires=timezone.now() + timedelta(days=2))
            record = ApplicationAccountInvitation.objects.create(invitation=invitation, deployment=deployment,
                name=name.strip(), role_ids=body['roleIds'])
            audit(principal, deployment, 'application.account_invited', invitation.pk)
            return {**envelope, 'invitation': invitation_output(record),
                'invitationUrl': settings.ISSUER + reverse('accept_invitation', args=[secret])}, 200
        if operation == 'revoke':
            if set(body) != {'id'} or not isinstance(body['id'], str) or not re.fullmatch(r'[1-9][0-9]{0,18}', body['id']): raise ValidationError('Choose an invitation.')
            record = ApplicationAccountInvitation.objects.select_related('invitation').filter(deployment=deployment, invitation_id=body['id']).first()
            if not record: raise Http404
            from .access import require_access
            require_access(identity_principal, record.invitation, 'application.invitation_revoke')
            if record.invitation.accepted: return {'error': 'invitation_accepted'}, 409
            if not record.revoked_at:
                record.revoked_at = timezone.now(); record.save(update_fields=['revoked_at'])
                record.invitation.expires = timezone.now(); record.invitation.save(update_fields=['expires'])
                audit(principal, deployment, 'application.account_invitation_revoked', record.invitation_id)
            return {**envelope, 'revoked': True}, 200
        raise ValidationError('Unsupported account operation.')


@csrf_exempt
@require_POST
def application_accounts(request):
    from .views import machine_response
    try:
        if int(request.META.get('CONTENT_LENGTH') or 0) > 20000: raise ValidationError('Account request is too large.')
        result, status = run_operation(request)
    except ValidationError:
        result, status = {'error': 'invalid_account_request'}, 400
    except PermissionDenied:
        result, status = {'error': 'account_management_denied'}, 403
    except Http404:
        result, status = {'error': 'invitation_not_found'}, 404
    return machine_response(result, status)
