"""OIDC authorization-code broker; validated external identity never grants Core roles."""
import base64
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone as utc
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import jwt
import requests
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import AuthenticationEvidence, AuditEvent, FederatedIdentity, IdentityProvider, Membership


class FederationError(Exception):
    pass


def trusted_url(value):
    parsed = urlparse(value)
    defaults = {"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com", "login.microsoftonline.com"}
    cloud = bool(re.fullmatch(r"cognito-idp\.[a-z0-9-]+\.amazonaws\.com", parsed.hostname or ""))
    approved = parsed.hostname in defaults or parsed.hostname in settings.CORE_IDP_ALLOWED_HOSTS or cloud
    local = settings.DEBUG and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not parsed.netloc or parsed.username is not None or parsed.query or parsed.fragment or
            not (approved or local) or not (parsed.scheme == "https" or local and parsed.scheme == "http")):
        raise ValidationError("Use an HTTPS provider URL on an operator-approved identity provider host.")
    return value


def secret_path(provider):
    root = Path(settings.CORE_IDP_SECRET_ROOT).resolve()
    scoped_root = root / str(provider.organization_id)
    path = Path(provider.client_secret_file)
    if not path.is_absolute() or not path.resolve().is_relative_to(scoped_root) or path.resolve() == scoped_root:
        raise ValidationError("The client secret must be mounted inside CORE_IDP_SECRET_ROOT/<organization-uuid>/.")
    return path.resolve()


def validate_provider(provider):
    trusted_url(provider.issuer)
    if provider.issuer.endswith("/"):
        raise ValidationError("Store the exact issuer without a trailing slash.")
    secret_path(provider)
    for values in [provider.mfa_amr, provider.mfa_acr]:
        if not isinstance(values, list) or len(values) > 20 or any(not isinstance(v, str) or not 1 <= len(v) <= 255 for v in values):
            raise ValidationError("MFA assurance mappings require lists of exact claim values.")
    if any(value in {"pwd", "password", "none", "0"} for value in provider.mfa_amr):
        raise ValidationError("Password-only indicators cannot establish MFA.")


def http_json(method, url, **kwargs):
    try:
        trusted_url(url)
        with requests.request(method, url, timeout=10, allow_redirects=False, stream=True, **kwargs) as response:
            if response.status_code != 200:
                raise FederationError("Provider request rejected.")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 1048576:
                    raise FederationError("Provider response too large.")
                chunks.append(chunk)
            value = json.loads(b"".join(chunks))
            if not isinstance(value, dict):
                raise FederationError("Invalid provider document.")
            return value
    except (requests.RequestException, ValidationError, ValueError):
        raise FederationError("Provider request failed.") from None


def discovery(provider):
    document = http_json("GET", provider.issuer + "/.well-known/openid-configuration")
    if document.get("issuer") != provider.issuer:
        raise FederationError("Provider issuer mismatch.")
    for key in ["authorization_endpoint", "token_endpoint", "jwks_uri"]:
        if not isinstance(document.get(key), str):
            raise FederationError("Incomplete provider metadata.")
        try:
            trusted_url(document[key])
        except ValidationError:
            raise FederationError("Untrusted provider endpoint.") from None
    return document


def verify_id_token(provider, document, raw, nonce):
    try:
        if not isinstance(raw, str) or len(raw) > 32768:
            raise FederationError("Invalid identity token.")
        header = jwt.get_unverified_header(raw)
        algorithm = header.get("alg")
        if algorithm not in {"RS256", "ES256"} or not isinstance(header.get("kid"), str):
            raise FederationError("Unsupported identity signature.")
        keys = http_json("GET", document["jwks_uri"]).get("keys")
        if not isinstance(keys, list) or len(keys) > 50:
            raise FederationError("Invalid signing keys.")
        matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"] and
                   key.get("use", "sig") == "sig" and key.get("alg", algorithm) == algorithm]
        if len(matches) != 1:
            raise FederationError("Ambiguous signing key.")
        key = jwt.PyJWK.from_dict(matches[0], algorithm=algorithm)
        claims = jwt.decode(raw, key.key, algorithms=[algorithm], audience=provider.client_id, issuer=provider.issuer,
                            leeway=30, options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
        if (not isinstance(claims["nonce"], str) or not secrets.compare_digest(claims["nonce"], nonce) or
            not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255 or
            ("azp" in claims and claims["azp"] != provider.client_id) or
            (isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != provider.client_id)):
            raise FederationError("Identity token binding failed.")
        if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
            raise FederationError("Invalid token timestamps.")
        auth_time = claims.get("auth_time")
        if "auth_time" in claims and (type(auth_time) is not int or auth_time > time.time() + 30 or auth_time < 0):
            raise FederationError("Invalid authentication time.")
        amr = claims.get("amr", [])
        acr = claims.get("acr", "")
        if not isinstance(amr, list) or any(not isinstance(v, str) for v in amr) or not isinstance(acr, str) or len(acr) > 255:
            raise FederationError("Invalid authentication assurance.")
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise FederationError("Identity token validation failed.") from None


def safe_next(request, value):
    return value if value and url_has_allowed_host_and_scheme(value, {request.get_host()}, require_https=not settings.DEBUG) else "/"


class ProviderLoginView(LoginView):
    template_name = "registration/login.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .branding import context as brand
        organization = brand(self.request)["brand_organization"]
        slug = self.request.GET.get("organization")
        providers = IdentityProvider.objects.filter(active=True, organization__active=True)
        if organization:
            providers = providers.filter(organization=organization)
        elif slug:
            providers = providers.filter(organization__slug=slug)
        context["identity_providers"] = providers.select_related("organization")
        return context


@never_cache
@require_GET
def begin(request, pk):
    provider = get_object_or_404(IdentityProvider, pk=pk, active=True, organization__active=True)
    purpose = request.GET.get("purpose_code", "") or request.session.get("access_purposes", {}).get(str(provider.organization_id), "")
    if purpose and not re.fullmatch(r"[-a-zA-Z0-9_]{1,80}", purpose):
        return HttpResponseBadRequest("Use a configured access purpose code.")
    try:
        validate_provider(provider)
        document = discovery(provider)
        verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        request.session["oidc_attempt"] = {"provider": str(provider.pk), "version": provider.configuration_version,
            "state": state, "nonce": nonce, "verifier": verifier, "created": int(time.time()),
            "next": safe_next(request, request.GET.get("next")), "reauth": request.GET.get("reauth") == "1", "purpose_code": purpose}
        params = {"client_id": provider.client_id, "response_type": "code", "scope": "openid profile email",
            "redirect_uri": settings.ISSUER + reverse("provider_callback", args=[provider.pk]), "state": state, "nonce": nonce,
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
            "code_challenge_method": "S256"}
        if request.GET.get("reauth") == "1":
            params["max_age"] = "0"
        return redirect(document["authorization_endpoint"] + "?" + urlencode(params))
    except (FederationError, ValidationError):
        return render(request, "identity/federation_error.html", status=503)


@never_cache
@require_GET
def callback(request, pk):
    provider = get_object_or_404(IdentityProvider, pk=pk, active=True, organization__active=True)
    attempt = request.session.pop("oidc_attempt", None)
    if (not attempt or attempt["provider"] != str(provider.pk) or attempt["version"] != provider.configuration_version or
        not 0 <= time.time() - attempt["created"] <= 600 or not secrets.compare_digest(attempt["state"], request.GET.get("state", ""))):
        return HttpResponseBadRequest("This sign-in attempt is invalid or expired. Start again.")
    try:
        if request.GET.get("error") or not request.GET.get("code") or len(request.GET["code"]) > 4096:
            raise FederationError("Authorization failed.")
        document = discovery(provider)
        secret = secret_path(provider).read_text().strip()
        if not secret:
            raise FederationError("Missing client secret.")
        data = {"grant_type": "authorization_code", "code": request.GET["code"], "code_verifier": attempt["verifier"],
            "redirect_uri": settings.ISSUER + reverse("provider_callback", args=[provider.pk])}
        auth = None
        if provider.token_auth_method == "client_secret_post":
            data.update(client_id=provider.client_id, client_secret=secret)
        else:
            auth = (quote(provider.client_id, safe=""), quote(secret, safe=""))
        tokens = http_json("POST", document["token_endpoint"], data=data, auth=auth)
        claims = verify_id_token(provider, document, tokens.get("id_token"), attempt["nonce"])
        if attempt["reauth"] and ("auth_time" not in claims or time.time() - claims["auth_time"] > 60):
            raise FederationError("Fresh authentication was not established.")
        identity = FederatedIdentity.objects.select_related("user").filter(provider=provider, subject=claims["sub"], active=True).first()
        if (not identity or not identity.user.is_active or not Membership.objects.filter(user=identity.user,
                organization=provider.organization, active=True).exists()):
            raise FederationError("Account linking required.")
        # An existing browser account must not silently become a different account.
        if request.user.is_authenticated and request.user.pk != identity.user_id:
            raise FederationError("Sign out before using another account.")
        amr, acr = claims.get("amr", []), claims.get("acr", "")
        evidence = AuthenticationEvidence.objects.create(user=identity.user, identity=identity,
            provider_version=provider.configuration_version, amr=amr, acr=acr,
            mfa=bool(set(amr) & set(provider.mfa_amr) or acr and acr in provider.mfa_acr),
            authenticated_at=datetime.fromtimestamp(claims["auth_time"], tz=utc.utc) if "auth_time" in claims else None)
        from .access import authentication_valid
        if not authentication_valid(identity.user, provider.organization, evidence):
            evidence.revoked = True
            evidence.save(update_fields=["revoked"])
            raise FederationError("Organization authentication policy denied sign-in.")
        from .access import Principal, require_access
        from django.core.exceptions import PermissionDenied
        try:
            require_access(Principal(user=identity.user, evidence=evidence, purpose=attempt.get("purpose_code", ""),
                                     request_id=getattr(request, "security_request_id", None)), provider, "authentication.provider_access")
        except PermissionDenied:
            evidence.revoked = True
            evidence.save(update_fields=["revoked"])
            raise FederationError("Provider access policy denied sign-in.") from None
        request.verified_federation = evidence
        login(request, identity.user, backend="django.contrib.auth.backends.ModelBackend")
        choices = dict(request.session.get("access_purposes", {}))
        choices[str(provider.organization_id)] = attempt.get("purpose_code", "")
        request.session["access_purposes"] = choices
        return redirect(attempt["next"])
    except (FederationError, ValidationError, OSError, OverflowError):
        AuditEvent.objects.create(organization=provider.organization, action="authentication.federation_denied", target=str(provider.pk),
            outcome="denied", reason_code="federation_failed", request_id=getattr(request, "security_request_id", None))
        return render(request, "identity/federation_error.html", status=403)
