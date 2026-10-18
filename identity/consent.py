"""Remember explicit OAuth approval without turning it into an access grant."""
import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from oauth2_provider.exceptions import OAuthToolkitError
from oauth2_provider.models import (
    get_access_token_model, get_grant_model, get_id_token_model, get_refresh_token_model,
)
from oauth2_provider.views import AuthorizationView
from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error

from .models import Application, ApplicationConsent, AuditEvent

CLAIMS_MAX_BYTES = 16 * 1024
CLAIMS_MAX_DEPTH = 8


def valid_claims_request(raw):
    try:
        if len(raw.encode("utf-8")) > CLAIMS_MAX_BYTES:
            return False
        claims = json.loads(raw)
        if not isinstance(claims, dict) or set(claims) - {"id_token", "userinfo"}:
            return False
        pending = [(claims, 1)]
        while pending:
            item, depth = pending.pop()
            if isinstance(item, (dict, list)):
                if depth > CLAIMS_MAX_DEPTH:
                    return False
                children = item.values() if isinstance(item, dict) else item
                pending.extend((child, depth + 1) for child in children)
        for section in claims.values():
            if not isinstance(section, dict):
                return False
            for name, options in section.items():
                if not isinstance(name, str) or not (options is None or isinstance(options, dict)):
                    return False
                if isinstance(options, dict) and (
                    "essential" in options and not isinstance(options["essential"], bool) or
                    "values" in options and not isinstance(options["values"], list)
                ):
                    return False
        return True
    except (TypeError, ValueError, RecursionError):
        return False


def canonical_scope(scopes):
    return " ".join(sorted(set(scopes.split() if isinstance(scopes, str) else scopes)))


def registration_fingerprint(application, redirect_uri, claims=None):
    """A changed recipient, callback, requested claims, or placement needs approval."""
    from platform_services.external_apps import application_deployment
    deployment = application_deployment(application)
    # DOT persists an absent claims request as JSON {}, rather than None.
    if claims is None:
        claims = {}
    registration = {
        "client_id": application.client_id,
        "client_type": application.client_type,
        "grant_type": application.authorization_grant_type,
        "algorithm": application.algorithm,
        "name": application.name,
        "service_id": str(application.service_id),
        "organization_id": str(application.service.organization_id),
        "redirect_uris": sorted(application.redirect_uris.split()),
        "allowed_origins": sorted(application.allowed_origins.split()),
        "redirect_uri": redirect_uri,
        "claims": claims,
        "deployment_id": str(deployment.pk) if deployment else None,
        "environment_id": str(deployment.environment_id) if deployment else None,
    }
    return hashlib.sha256(json.dumps(registration, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RememberedConsentAuthorizationView(AuthorizationView):
    def get_initial(self):
        initial = super().get_initial()
        # DOT renders an absent claims request as the JSON string "null".
        # Preserve absence rather than posting a scalar claims value back.
        if initial.get("claims") in (None, "null"):
            initial["claims"] = ""
        return initial

    def dispatch(self, request, *args, **kwargs):
        params = request.POST if request.method == "POST" else request.GET
        if any(len(values) > 1 for _, values in params.lists()):
            return JsonResponse({"error": "invalid_request", "error_description": "Repeated authorization parameters are not accepted."}, status=400)
        if params.get("claims") and not valid_claims_request(params["claims"]):
            return JsonResponse({"error": "invalid_request", "error_description": "Claims must contain bounded OIDC claim objects."}, status=400)
        if (not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", "")) or
                params.get("code_challenge_method") != "S256"):
            return JsonResponse({"error": "invalid_request", "error_description": "PKCE requires an S256 challenge."}, status=400)
        application = Application.objects.filter(client_id=params.get("client_id")).first()
        # The model disallows this flag. Enforce that invariant even for legacy/raw writes.
        if application and application.skip_authorization:
            return JsonResponse({"error": "unauthorized_client"}, status=400)
        return super().dispatch(request, *args, **kwargs)

    def handle_no_permission(self):
        if self.request.method == "GET" and "none" in self.request.GET.get("prompt", "").split():
            # Validate the client/callback before returning a silent error. Do
            # this before LoginRequiredMixin can send the browser to a login UI.
            try:
                _, credentials = self.validate_authorization_request(self.request)
            except OAuthToolkitError as error:
                return self.error_response(error, application=None)
            application = Application.objects.get(client_id=credentials["client_id"])
            error = OAuthToolkitError(error=CustomOAuth2Error(error="login_required", state=credentials.get("state")),
                                      redirect_uri=credentials["redirect_uri"])
            return self.error_response(error, application)
        return super().handle_no_permission()

    def validate_authorization_request(self, request):
        validated = super().validate_authorization_request(request)
        self.validated_authorization_request = validated
        return validated

    def get(self, request, *args, **kwargs):
        original_params = request.GET
        # DOT's auto mode trusts an unexpired access token. A saved approval is
        # required here instead, including after token expiry or portal logout.
        params = original_params.copy()
        params["approval_prompt"] = "force"
        request.GET = params
        try:
            response = super().get(request, *args, **kwargs)
        finally:
            request.GET = original_params
        context = getattr(response, "context_data", None)
        if response.status_code != 200 or not context or "application" not in context or context.get("error"):
            return response
        prompt = set(original_params.get("prompt", "").split())
        if original_params.get("approval_prompt") == "force" or "consent" in prompt:
            return response
        scopes, credentials = self.validated_authorization_request
        application = context["application"]
        with transaction.atomic():
            approved = ApplicationConsent.objects.select_for_update().filter(
                user=request.user, application=application, revoked_at__isnull=True,
                scope=canonical_scope(scopes),
                registration_fingerprint=registration_fingerprint(application, credentials["redirect_uri"], credentials.get("claims")),
            ).first()
            if not approved:
                if "none" in prompt:
                    error = OAuthToolkitError(error=CustomOAuth2Error(error="consent_required", state=credentials.get("state")),
                                              redirect_uri=credentials["redirect_uri"])
                    return self.error_response(error, application)
                return response
            # Revoke locks this same row before removing credentials. A racing
            # no-prompt authorization cannot issue a code after revocation.
            try:
                uri, _, _, _ = self.create_authorization_response(
                    request=request, scopes=" ".join(scopes), credentials=credentials, allow=True)
            except OAuthToolkitError as error:
                return self.error_response(error, application)
            return self.redirect(uri, application)

    def form_valid(self, form):
        application = Application.objects.select_related("service__organization").filter(client_id=form.cleaned_data["client_id"]).first()
        if application is None:
            return JsonResponse({"error": "invalid_client"}, status=400)
        with transaction.atomic():
            response = super().form_valid(form)
            if form.cleaned_data.get("allow") and response.status_code == 302:
                code = parse_qs(urlparse(response["Location"]).query).get("code", [None])[0]
                grant = get_grant_model().objects.filter(code=code, application=application, user=self.request.user).first()
                # Persist only a real code grant issued by oauthlib after validation.
                if grant:
                    ApplicationConsent.objects.update_or_create(user=self.request.user, application=application, defaults={
                        "scope": canonical_scope(grant.scope), "revoked_at": None, "granted_at": timezone.now(),
                        "registration_fingerprint": registration_fingerprint(application, grant.redirect_uri, json.loads(grant.claims or "{}")),
                    })
                    AuditEvent.objects.create(organization=application.service.organization, actor=self.request.user,
                        action="oauth.consent_granted", target=str(application.pk),
                        request_id=getattr(self.request, "security_request_id", None))
            return response


@login_required
@never_cache
@require_POST
def revoke_application_consent(request, pk):
    with transaction.atomic():
        consent = get_object_or_404(ApplicationConsent.objects.select_for_update().select_related("application__service__organization"),
                                    pk=pk, user=request.user)
        consent.revoked_at = timezone.now()
        consent.save(update_fields=["revoked_at"])
        # Revoke this user's credentials for this app, including unredeemed codes.
        for model in [get_grant_model(), get_refresh_token_model(), get_access_token_model(), get_id_token_model()]:
            model.objects.filter(user=request.user, application=consent.application).delete()
        AuditEvent.objects.create(organization=consent.application.service.organization, actor=request.user,
            action="oauth.consent_revoked", target=str(consent.application_id), request_id=getattr(request, "security_request_id", None))
    messages.success(request, "Application approval revoked. The next sign-in will ask for approval again.")
    return redirect("profile")
