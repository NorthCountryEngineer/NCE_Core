from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error, InvalidGrantError
from .policy import authorization_for, membership_for
from .access import Principal, binding_for, decision, record_decision
from .models import ApplicationConsent, AuthenticationEvidence, AuditEvent, OAuthEvidence
import hashlib


class OrganizationValidator(OAuth2Validator):
    oidc_claim_scope = {
        **OAuth2Validator.oidc_claim_scope,
        "organization_id": "openid", "service_id": "openid", "identity_name": "openid",
        "roles": "authorization", "permissions": "authorization",
        "amr": "openid", "acr": "openid", "auth_time": "openid",
        'environment': 'openid', 'deployment_id': 'openid',
    }

    def validate_scopes(self, client_id, scopes, client, request, *args, **kwargs):
        return "openid" in scopes and super().validate_scopes(client_id, scopes, client, request, *args, **kwargs)

    def validate_silent_login(self, request):
        evidence = AuthenticationEvidence.objects.select_related("user").filter(
            pk=request.headers.get("core_authentication_evidence") or None).first()
        if not evidence:
            return False
        request.user = evidence.user
        request.authentication_evidence = evidence
        request.authentication_purpose = request.headers.get("core_purpose_code", "")
        return bool(membership_for(request.user, request.client.service) and self.client_access(request, request.client))

    def validate_silent_authorization(self, request):
        from .consent import canonical_scope, registration_fingerprint
        import json
        claims = request.claims
        if isinstance(claims, str):
            claims = json.loads(claims)
        return bool(getattr(request, "approval_prompt", None) != "force" and ApplicationConsent.objects.filter(
            user=request.user, application=request.client, revoked_at__isnull=True,
            scope=canonical_scope(request.scopes),
            registration_fingerprint=registration_fingerprint(request.client, request.redirect_uri, claims),
        ).exists())

    def _check_and_set_request_resource(self, request):
        if getattr(request, "resource", None):
            raise CustomOAuth2Error(error="invalid_target", description="The token audience is fixed by the registered service.", request=request)
        super()._check_and_set_request_resource(request)

    def bind_request(self, raw, kind, request):
        binding = binding_for(raw, kind)
        request.authentication_evidence = binding.evidence if binding else None
        request.authentication_purpose = binding.purpose_code if binding else ""
        return request.authentication_evidence

    def client_access(self, request, client):
        principal = Principal(user=request.user, evidence=request.authentication_evidence, purpose=request.authentication_purpose,
                              request_id=request.headers.get("core_request_id") or None, application=client)
        # Missing rows preserve existing credentials. A revoked approval is a
        # live denial, including tokens minted by an already in-flight refresh.
        consent = ApplicationConsent.objects.filter(user=request.user, application=client).first()
        request.core_consent_granted_at = consent.granted_at if consent else None
        if consent and consent.revoked_at:
            record_decision(principal, client, "oauth.client_access", False, "consent_revoked", [])
            return False
        allowed, reason, tags = decision(principal, client)
        if tags or not allowed:
            record_decision(principal, client, "oauth.client_access", allowed, reason, tags)
        return allowed

    def validate_code(self, client_id, code, client, request, *args, **kwargs):
        valid = super().validate_code(client_id, code, client, request, *args, **kwargs)
        self.bind_request(code, "code", request)
        return bool(valid and request.authentication_evidence and membership_for(request.user, client.service) is not None and
                    self.client_access(request, client))

    def validate_refresh_token(self, refresh_token, client, request, *args, **kwargs):
        valid = super().validate_refresh_token(refresh_token, client, request, *args, **kwargs)
        self.bind_request(refresh_token, "refresh", request)
        return bool(valid and request.authentication_evidence and membership_for(request.user, client.service) is not None and
                    self.client_access(request, client))

    def validate_bearer_token(self, token, scopes, request):
        valid = super().validate_bearer_token(token, scopes, request)
        self.bind_request(token, "access", request)
        return bool(valid and request.client is not None and membership_for(request.user, request.client.service) is not None and
                    self.client_access(request, request.client))

    def save_authorization_code(self, client_id, code, request, *args, **kwargs):
        super().save_authorization_code(client_id, code, request, *args, **kwargs)
        evidence = AuthenticationEvidence.objects.filter(pk=request.headers.get("core_authentication_evidence") or None,
                                                        user=request.user).first()
        if evidence:
            OAuthEvidence.objects.create(token_hash=hashlib.sha256(code["code"].encode()).hexdigest(), kind="code", evidence=evidence,
                                         purpose_code=request.headers.get("core_purpose_code", ""))

    def _save_bearer_token(self, token, request, *args, **kwargs):
        # DOT calls this inside its token-write transaction. Order this lock
        # before token/family locks, just as the user revoke endpoint does.
        consent = ApplicationConsent.objects.select_for_update().filter(user=request.user, application=request.client).first()
        if consent and (consent.revoked_at or consent.granted_at != getattr(request, "core_consent_granted_at", None)):
            raise InvalidGrantError(request=request)
        super()._save_bearer_token(token, request, *args, **kwargs)
        evidence = getattr(request, "authentication_evidence", None)
        if evidence:
            for field, kind in [("access_token", "access"), ("refresh_token", "refresh")]:
                if token.get(field):
                    OAuthEvidence.objects.update_or_create(token_hash=hashlib.sha256(token[field].encode()).hexdigest(),
                                                          defaults={"evidence": evidence, "kind": kind,
                                                                    "purpose_code": getattr(request, "authentication_purpose", "")})
        AuditEvent.objects.create(organization=request.client.service.organization, actor=request.user,
                                 action="oauth.token_issued", target=str(request.client.pk))

    def get_additional_claims(self, request):
        service = request.client.service
        membership = membership_for(request.user, service)
        claims = {
            "email": request.user.email,
            "name": request.user.get_full_name(),
            "given_name": request.user.first_name,
            "family_name": request.user.last_name,
            "organization_id": str(service.organization_id),
            "service_id": str(service.pk),
            "identity_name": service.organization.brand,
        }
        if request.user.avatar_url:
            claims["picture"] = request.user.avatar_url
        deployment = getattr(request.client, 'deployment', None)
        if deployment and deployment.environment_id:
            claims.update({'environment': deployment.environment.name, 'deployment_id': str(deployment.pk)})
        if membership:
            claims.update(authorization_for(membership, service, Principal(user=request.user,
                evidence=getattr(request, "authentication_evidence", None), purpose=getattr(request, "authentication_purpose", ""), application=request.client)))
        evidence = getattr(request, "authentication_evidence", None)
        claims["amr"] = ["mfa"] if evidence and evidence.mfa else ["pwd"] if evidence and not evidence.identity_id else []
        claims["acr"] = "urn:core:assurance:mfa" if evidence and evidence.mfa else "urn:core:assurance:single-factor"
        if evidence and evidence.authenticated_at:
            claims["auth_time"] = int(evidence.authenticated_at.timestamp())
        return claims

    def get_discovery_claims(self, request):
        return ["sub", "name", "given_name", "family_name", "picture", "email", "organization_id", "service_id", "identity_name", "roles", "permissions", "amr", "acr", "auth_time", 'environment', 'deployment_id']
