from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver
from django.utils import timezone

from .models import AuditEvent, AuthenticationEvidence, Membership


@receiver(user_logged_in)
def track_login(sender, request, user, **kwargs):
    evidence = getattr(request, "verified_federation", None)
    if evidence is None:
        evidence = AuthenticationEvidence.objects.create(user=user, authenticated_at=timezone.now(), amr=["pwd"])
    request.session["authentication_evidence"] = str(evidence.pk)
    request.session.set_expiry(28800)
    for membership in Membership.objects.filter(user=user, active=True, organization__active=True):
        AuditEvent.objects.create(organization=membership.organization, actor=user, action="authentication.succeeded", target=str(evidence.pk),
            request_id=getattr(request, "security_request_id", None))


@receiver(user_logged_out)
def track_logout(sender, request, user, **kwargs):
    if request:
        evidence = request.session.get("authentication_evidence")
        if evidence:
            AuthenticationEvidence.objects.filter(pk=evidence, user=user).update(revoked=True)
            if user:
                for membership in Membership.objects.filter(user=user, active=True):
                    AuditEvent.objects.create(organization=membership.organization, actor=user, action="authentication.signed_out",
                        target=str(evidence), request_id=getattr(request, "security_request_id", None))


@receiver(user_login_failed)
def track_login_denied(sender, credentials, request=None, **kwargs):
    from .models import User
    email = credentials.get("username", credentials.get("email", ""))
    user = User.objects.filter(email=str(email).strip().lower()).first()
    if user:
        for membership in Membership.objects.filter(user=user, active=True, organization__active=True):
            AuditEvent.objects.create(organization=membership.organization, action="authentication.local_denied", target=str(user.pk),
                outcome="denied", reason_code="invalid_credentials", request_id=getattr(request, "security_request_id", None))
