import base64
import binascii
import hashlib
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import unquote, urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods
from oauth2_provider.models import get_access_token_model
from oauth2_provider.views import ConnectDiscoveryInfoView

from .forms import (
    AcceptInvitationForm, ApplicationForm, BrandingForm, InvitationForm, MembershipForm,
    PermissionForm, ProfileForm, RoleForm, ServiceForm,
)
from .client_credentials import resource_client_secrets
from .consent import RememberedConsentAuthorizationView
from .models import Application, ApplicationConsent, AuditEvent, Invitation, Membership, Organization, Role, Service, ServicePermission, User
from .policy import authorization_for, membership_for
from .policy_evaluation import policy_evaluation


def audit(request, action, target):
    from .access import request_principal
    import re
    purpose = request_principal(request).purpose
    AuditEvent.objects.create(organization=request.organization, actor=request.user, action=action, target=str(target),
                              request_id=getattr(request, "security_request_id", None),
                              purpose_code=purpose if re.fullmatch(r"[-a-zA-Z0-9_]{1,80}", purpose or "") else "")


def organization_admin(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapped(request, slug, *args, **kwargs):
        organization = get_object_or_404(Organization, slug=slug, active=True)
        allowed = request.user.is_superuser or organization.memberships.filter(
            user=request.user, active=True, kind=Membership.Kind.ADMINISTRATOR,
        ).exists()
        if not allowed:
            raise PermissionDenied
        request.organization = organization
        from .security_portal import enforce_portal_access
        enforce_portal_access(request, organization, view.__name__, kwargs)
        return view(request, organization, *args, **kwargs)
    return wrapped


@login_required
@never_cache
def home(request):
    organizations = Organization.objects.filter(active=True)
    if not request.user.is_superuser:
        organizations = organizations.filter(memberships__user=request.user, memberships__active=True)
    organizations = list(organizations)
    admin_ids = set(request.user.memberships.filter(
        active=True, kind=Membership.Kind.ADMINISTRATOR,
    ).values_list("organization_id", flat=True))
    for organization in organizations:
        organization.can_administer = request.user.is_superuser or organization.pk in admin_ids
        organization.access_purpose = request.session.get("access_purposes", {}).get(str(organization.pk), "")
    return render(request, "identity/home.html", {"organizations": organizations})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def profile(request):
    # Keep a failed form's unvalidated URL out of the image preview. ModelForm
    # constructs its instance before all validation errors have been resolved.
    saved_avatar_url = request.user.avatar_url
    form = ProfileForm(request.POST if request.method == "POST" else None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        user = form.save(commit=False)
        # Persist only owner-editable metadata; a stale request cannot overwrite
        # a concurrent operator change to email, password, status, or privileges.
        user.save(update_fields=ProfileForm._meta.fields)
        messages.success(request, "Your profile has been saved.")
        return redirect("profile")
    return render(request, "identity/profile.html", {
        "form": form, "title": "Your profile", "saved_avatar_url": saved_avatar_url,
        "application_consents": request.user.application_consents.filter(revoked_at__isnull=True).select_related(
            "application__service__organization").order_by("application__name"),
    })


@organization_admin
def organization_detail(request, organization):
    from .access import request_principal, visible_resources
    principal = request_principal(request)
    return render(request, "identity/organization.html", {
        "organization": organization,
        "memberships": visible_resources(principal, organization.memberships.select_related("user").prefetch_related("roles")),
        "services": visible_resources(principal, organization.services.all()), "roles": visible_resources(principal, organization.roles.all()),
        "invitations": visible_resources(principal, organization.invitations.filter(accepted=False, expires__gt=timezone.now())),
        "audit_events": organization.audit_events.select_related("actor")[:30],
    })


def edit_form(request, form, title, target, action):
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                instance = form.save()
                audit(request, action, instance.pk)
        except IntegrityError:
            form.add_error(None, "This name already exists in this organization.")
        except ValidationError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "Saved.")
            return redirect(target)
    return render(request, "identity/form.html", {"form": form, "title": title})


@organization_admin
def branding(request, organization):
    return edit_form(request, BrandingForm(request.POST or None, instance=organization),
                     "Organization settings", reverse("organization", args=[organization.slug]), "organization.updated")


@organization_admin
def invite(request, organization):
    form = InvitationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        raw = secrets.token_urlsafe(32)
        with transaction.atomic():
            invitation = form.save(commit=False)
            invitation.organization = organization
            invitation.token_hash = Invitation.digest(raw)
            invitation.expires = timezone.now() + timedelta(days=2)
            invitation.save()
            audit(request, "invitation.created", invitation.pk)
        link = settings.ISSUER + reverse("accept_invitation", args=[raw])
        response = render(request, "identity/credential.html", {
            "title": "Invitation created", "invitation_link": link,
            "description": "Share this single-use link securely with the invited person. It expires in 48 hours.",
        })
        response["Referrer-Policy"] = "same-origin"
        return response
    return render(request, "identity/form.html", {"form": form, "title": "Invite a user"})


@organization_admin
@require_POST
def revoke_invitation(request, organization, pk):
    with transaction.atomic():
        # Match acceptance and directory edits before SET_NULL can lock a person.
        Organization.objects.select_for_update().get(pk=organization.pk)
        invitation = get_object_or_404(organization.invitations.select_for_update(), pk=pk)
        invitation.delete()
        audit(request, "invitation.revoked", pk)
    return redirect("organization", slug=organization.slug)


@never_cache
def accept_invitation(request, token):
    from .app_auth import authorization_context
    continuation = request.POST.get("next", request.GET.get("next", ""))
    app_context = authorization_context(request, continuation) if continuation else None
    if continuation and not app_context:
        return HttpResponse("Start account creation again from the application.", status=400)
    invitation_organization = get_object_or_404(
        Invitation.objects.only("organization_id"), token_hash=Invitation.digest(token),
    ).organization_id
    with transaction.atomic():
        # Directory and membership mutations share this lock order: organization,
        # then invitation/person. Revalidate the token after acquiring the lock.
        Organization.objects.select_for_update().get(pk=invitation_organization)
        invitation = get_object_or_404(
            Invitation.objects.select_for_update().select_related("organization"),
            token_hash=Invitation.digest(token), organization_id=invitation_organization,
        )
        if not invitation.usable:
            return HttpResponse("This invitation has expired or has already been used.", status=410)
        request.organization = invitation.organization
        if app_context and app_context["application"].service.organization_id != invitation.organization_id:
            from django.http import Http404
            raise Http404("This invitation is unavailable for this application.")
        existing = User.objects.filter(email__iexact=invitation.email).first()
        if existing:
            if not request.user.is_authenticated:
                return redirect(settings.LOGIN_URL + "?" + urlencode({"next": request.get_full_path()}))
            if request.user.pk != existing.pk or not existing.is_active:
                raise PermissionDenied("Sign in with the invited account.")
            form = None
            valid = request.method == "POST"
            user = existing
        else:
            user = User(email=invitation.email)
            form = AcceptInvitationForm(request.POST or None, instance=user)
            valid = request.method == "POST" and form.is_valid()
        if valid:
            if not existing:
                try:
                    with transaction.atomic():
                        user = form.save()
                except IntegrityError:
                    form.add_error(None, "This account now exists. Sign in before accepting the invitation.")
                    valid = False
            if valid:
                membership, created = Membership.objects.get_or_create(
                    organization=invitation.organization, user=user,
                    defaults={"kind": invitation.kind},
                )
                # An invitation never silently overwrites an existing membership or reactivates it.
                if not membership.active:
                    raise PermissionDenied("Ask an organization administrator to reactivate your membership.")
                from .access import Principal, request_principal
                from .directory import link_accepted_invitation
                if request.user.is_authenticated and request.user.pk == user.pk:
                    acceptance_principal = request_principal(request)
                else:
                    selected_purpose = request.session.get("access_purposes", {}).get(str(invitation.organization_id), "")
                    purpose = (request.headers.get("X-Core-Purpose", "") or request.POST.get("purpose_code", "") or
                        request.GET.get("purpose_code", "") or selected_purpose)
                    acceptance_principal = Principal(user=user, purpose=purpose,
                        request_id=getattr(request, "security_request_id", None))
                link_accepted_invitation(invitation, membership, principal=acceptance_principal)
                from .app_accounts import accept_application_invitation
                account_origin = accept_application_invitation(invitation, membership,
                    app_context["application"] if app_context else None)
                invitation.accepted = True
                invitation.save(update_fields=["accepted"])
                AuditEvent.objects.create(organization=invitation.organization, actor=user,
                                          action="invitation.accepted", target=str(membership.pk))
                if not existing:
                    login(request, user)
                if app_context:
                    from .access import decision, request_principal
                    principal = request_principal(request)
                    principal.application = app_context["application"]
                    if decision(principal, app_context["application"])[0]:
                        return redirect(app_context["next"])
                    return redirect(app_context["home"] + "/?account=created")
                if account_origin:
                    return redirect(account_origin + "/studio?account=created")
                return redirect("home")
        response = render(request, "identity/accept_invitation.html", {"form": form, "invitation": invitation, "next": continuation})
    response["Referrer-Policy"] = "same-origin"
    return response


@organization_admin
def membership_edit(request, organization, pk):
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        member = get_object_or_404(organization.memberships.select_for_update().select_related("user"), pk=pk)
        was_admin = member.active and member.kind == Membership.Kind.ADMINISTRATOR and member.user.is_active
        form = MembershipForm(request.POST or None, instance=member, organization=organization)
        if request.method == "POST" and form.is_valid():
            remains_admin = form.cleaned_data["active"] and form.cleaned_data["kind"] == Membership.Kind.ADMINISTRATOR
            if was_admin and not remains_admin and not organization.memberships.filter(
                active=True, kind=Membership.Kind.ADMINISTRATOR, user__is_active=True,
            ).exclude(pk=member.pk).exists():
                form.add_error(None, "Keep at least one active organization administrator.")
            else:
                form.save()
                audit(request, "membership.updated", member.pk)
                return redirect("organization", slug=organization.slug)
    return render(request, "identity/form.html", {"form": form, "title": f"Membership: {member.user.email}"})


@organization_admin
def service_edit(request, organization, pk=None):
    service = get_object_or_404(organization.services, pk=pk) if pk else Service(organization=organization)
    return edit_form(request, ServiceForm(request.POST or None, instance=service), "Service settings",
                     reverse("organization", args=[organization.slug]), "service.updated")


@organization_admin
def service_detail(request, organization, pk):
    service = get_object_or_404(organization.services, pk=pk)
    from .access import request_principal, visible_resources
    return render(request, "identity/service.html", {"service": service,
        "applications": visible_resources(request_principal(request), service.applications.all()),
        "permissions": visible_resources(request_principal(request), service.permissions.all()),
        "protected_resources": visible_resources(request_principal(request), service.protected_resources.all())})


@organization_admin
def permission_add(request, organization, pk):
    service = get_object_or_404(organization.services, pk=pk)
    return edit_form(request, PermissionForm(request.POST or None, instance=ServicePermission(service=service)),
                     "Register a service permission", reverse("service", args=[organization.slug, pk]), "permission.created")


@organization_admin
def role_edit(request, organization, pk=None):
    role = get_object_or_404(organization.roles, pk=pk) if pk else Role(organization=organization)
    return edit_form(request, RoleForm(request.POST or None, instance=role, organization=organization),
                     "Role settings", reverse("organization", args=[organization.slug]), "role.updated")


@organization_admin
@never_cache
def application_edit(request, organization, service_pk, pk=None):
    service = get_object_or_404(organization.services, pk=service_pk)
    application = get_object_or_404(service.applications, pk=pk) if pk else Application(service=service)
    form = ApplicationForm(request.POST or None, instance=application)
    if request.method == "POST" and form.is_valid():
        # The secret is visible once, before the toolkit hashes it on save.
        raw_secret = application.client_secret if not pk else None
        with transaction.atomic():
            form.save()
            audit(request, "client.updated" if pk else "client.created", application.client_id)
        if not pk:
            return render(request, "identity/credential.html", {
                "title": "Application registered", "client_id": application.client_id,
                "client_secret": raw_secret if application.client_type == Application.CLIENT_CONFIDENTIAL else None,
                "description": "Save these credentials securely. The client secret will not be shown again.",
            })
        return redirect("service", slug=organization.slug, pk=service_pk)
    return render(request, "identity/form.html", {"form": form, "title": "Application registration"})


@organization_admin
@require_POST
def rotate_client_secret(request, organization, service_pk, pk):
    with transaction.atomic():
        application = get_object_or_404(Application.objects.select_for_update(), pk=pk,
                                       service_id=service_pk, service__organization=organization)
        if application.client_type != Application.CLIENT_CONFIDENTIAL:
            raise PermissionDenied("Public clients have no usable client secret.")
        raw = secrets.token_urlsafe(48)
        application.client_secret = raw
        application.save(update_fields=["client_secret"])
        audit(request, "client.secret_rotated", application.client_id)
    return render(request, "identity/credential.html", {
        "title": "Client secret rotated", "client_id": application.client_id, "client_secret": raw,
        "description": "Update this client's deployment. The previous secret is invalid immediately.",
    })


class TenantAuthorizationView(RememberedConsentAuthorizationView):
    def dispatch(self, request, *args, **kwargs):
        client_id = request.POST.get("client_id") if request.method == "POST" else request.GET.get("client_id")
        application = Application.objects.select_related("service__organization").filter(client_id=client_id).first()
        if application:
            request.organization = application.service.organization
            if request.user.is_authenticated and membership_for(request.user, application.service) is None:
                raise PermissionDenied("You do not have an active membership for this application.")
            if request.user.is_authenticated:
                from .access import request_principal, require_access
                principal = request_principal(request)
                principal.application = application
                require_access(principal, application, "oauth.authorize")
        # Tokens are audience-bound by registration. Arbitrary resource indicators are not accepted.
        if request.GET.get("resource") or request.POST.get("resource"):
            return JsonResponse({"error": "invalid_target"}, status=400)
        params = request.POST if request.method == "POST" else request.GET
        if params.get("code_challenge_method") and params["code_challenge_method"] != "S256":
            return JsonResponse({"error": "invalid_request", "error_description": "PKCE must use S256."}, status=400)
        return super().dispatch(request, *args, **kwargs)


class DiscoveryView(ConnectDiscoveryInfoView):
    def get(self, request, *args, **kwargs):
        import json
        response = super().get(request, *args, **kwargs)
        data = json.loads(response.content)
        data.update({
            "introspection_endpoint": settings.ISSUER + reverse("oauth2_provider:introspect"),
            "introspection_endpoint_auth_methods_supported": ["client_secret_basic"],
            "id_token_signing_alg_values_supported": ["RS256"],
        })
        response = JsonResponse(data)
        response["Access-Control-Allow-Origin"] = "*"
        return response


def machine_response(data, status=200):
    response = JsonResponse(data, status=status)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    if status == 401:
        response["WWW-Authenticate"] = 'Basic realm="Core Identity"'
    return response


def authenticate_resource_client(request):
    header = request.headers.get("Authorization", "")
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.lower() != "basic":
            return None
        client_id, secret = base64.b64decode(encoded, validate=True).decode().split(":", 1)
        application = Application.objects.select_related("service__organization").filter(client_id=unquote(client_id)).first()
        if (application and application.client_type == Application.CLIENT_CONFIDENTIAL
                and application.is_usable(None) and resource_client_secrets.verify(
                    application.client_id, unquote(secret), application.client_secret)):
            return application
    except (ValueError, UnicodeDecodeError, binascii.Error):
        pass
    return None


@policy_evaluation()
def token_claims(raw, resource_client, purpose="", request_id=None):
    token = get_access_token_model().objects.select_related("application__service__organization", "user").filter(
        token_checksum=hashlib.sha256(raw.encode()).hexdigest(),
    ).first()
    if (not token or not token.is_valid() or not token.application or
            token.application.service_id != resource_client.service_id or
            not token.application.is_usable(None)):
        return None
    membership = membership_for(token.user, token.application.service)
    if membership is None:
        return None
    from platform_services.external_apps import application_deployment, external_available
    token_deployment = application_deployment(token.application)
    client_deployment = application_deployment(resource_client)
    if (token_deployment and token_deployment.target == 'external') or (client_deployment and client_deployment.target == 'external'):
        if (not token_deployment or not client_deployment or token_deployment.pk != client_deployment.pk or
                token_deployment.application_id != token.application_id or
                (resource_client.pk != token.application_id and client_deployment.resource_application_id != resource_client.pk) or
                not external_available(token_deployment)):
            return None
    if (token_deployment.environment_id if token_deployment else None) != (client_deployment.environment_id if client_deployment else None):
        return None
    from .access import Principal, require_access, evidence_for, record_decision
    principal = Principal(user=token.user, evidence=evidence_for(raw), purpose=purpose, request_id=request_id, application=token.application)
    if ApplicationConsent.objects.filter(user=token.user, application=token.application, revoked_at__isnull=False).exists():
        record_decision(principal, token.application, "oauth.introspection", False, "consent_revoked", [])
        return None
    try:
        require_access(principal, token.application, "oauth.introspection")
    except PermissionDenied:
        return None
    authorization = authorization_for(membership, token.application.service, principal) if "authorization" in token.scopes else {
        "roles": [], "permissions": [],
    }
    return {
        "active": True, "iss": settings.ISSUER, "sub": str(token.user_id),
        "client_id": token.application.client_id, "aud": token.application.service.audience,
        "organization_id": str(token.application.service.organization_id),
        "service_id": str(token.application.service_id),
        'environment': token_deployment.environment.name if token_deployment and token_deployment.environment_id else None,
        'deployment_id': str(token_deployment.pk) if token_deployment else None,
        "scope": token.scope, "token_type": "Bearer", "exp": int(token.expires.timestamp()),
        "authentication": {"mfa": bool(principal.evidence and principal.evidence.mfa),
            "method": "oidc" if principal.evidence and principal.evidence.identity_id else "local",
            "auth_time": int(principal.evidence.authenticated_at.timestamp()) if principal.evidence and principal.evidence.authenticated_at else None},
        **authorization,
    }


@csrf_exempt
@require_POST
def introspect(request):
    application = authenticate_resource_client(request)
    if not application:
        return machine_response({"error": "invalid_client"}, 401)
    if not request.POST.get("token"):
        return machine_response({"error": "invalid_request"}, 400)
    return machine_response(token_claims(request.POST["token"], application, request.POST.get("purpose_code", ""),
                                         getattr(request, "security_request_id", None)) or {"active": False})


@csrf_exempt
@require_POST
def authorize_permission(request):
    application = authenticate_resource_client(request)
    if not application:
        return machine_response({"error": "invalid_client"}, 401)
    if not request.POST.get("token") or not request.POST.get("permission") or not request.POST.get("organization_id"):
        return machine_response({"error": "invalid_request"}, 400)
    claims = token_claims(request.POST["token"], application, request.POST.get("purpose_code", ""), getattr(request, "security_request_id", None))
    permission = request.POST["permission"]
    # admin means service administration. It is not a wildcard for unknown permission names.
    allowed = bool(claims and claims["organization_id"] == request.POST["organization_id"] and
                   permission in claims["permissions"])
    from .access import Principal, evidence_for, record_decision, effective_tags
    resource = ServicePermission.objects.filter(service=application.service, codename=permission).first() or application.service
    evidence = evidence_for(request.POST["token"])
    principal = Principal(user=User.objects.filter(pk=claims["sub"]).first() if claims else evidence.user if evidence else None,
                          evidence=evidence, purpose=request.POST.get("purpose_code", ""),
                          request_id=getattr(request, "security_request_id", None), application=application)
    if request.POST.get("resource_type") or request.POST.get("resource_id"):
        from .models import ProtectedResource
        from .access import decision
        registered = ProtectedResource.objects.filter(service=application.service, resource_type=request.POST.get("resource_type"),
                                                      external_id=request.POST.get("resource_id"), active=True).first()
        allowed = bool(allowed and registered and decision(principal, registered)[0])
        if registered:
            resource = registered
    record_decision(principal, resource, "authorization.decision", allowed, "policy_satisfied" if allowed else "permission_denied", effective_tags(resource))
    return machine_response({"allowed": allowed, "subject": claims["sub"] if allowed else None})


def health(request):
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok"})
