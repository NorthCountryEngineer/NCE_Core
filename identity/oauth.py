from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error
from .policy import authorization_for, membership_for


class OrganizationValidator(OAuth2Validator):
    oidc_claim_scope = {
        **OAuth2Validator.oidc_claim_scope,
        "organization_id": "openid", "service_id": "openid", "identity_name": "openid",
        "roles": "authorization", "permissions": "authorization",
    }

    def validate_scopes(self, client_id, scopes, client, request, *args, **kwargs):
        return "openid" in scopes and super().validate_scopes(client_id, scopes, client, request, *args, **kwargs)

    def _check_and_set_request_resource(self, request):
        if getattr(request, "resource", None):
            raise CustomOAuth2Error(error="invalid_target", description="The token audience is fixed by the registered service.", request=request)
        super()._check_and_set_request_resource(request)

    def validate_code(self, client_id, code, client, request, *args, **kwargs):
        return (super().validate_code(client_id, code, client, request, *args, **kwargs)
                and membership_for(request.user, client.service) is not None)

    def validate_refresh_token(self, refresh_token, client, request, *args, **kwargs):
        return (super().validate_refresh_token(refresh_token, client, request, *args, **kwargs)
                and membership_for(request.user, client.service) is not None)

    def validate_bearer_token(self, token, scopes, request):
        return (super().validate_bearer_token(token, scopes, request)
                and request.client is not None
                and membership_for(request.user, request.client.service) is not None)

    def get_additional_claims(self, request):
        service = request.client.service
        membership = membership_for(request.user, service)
        claims = {
            "email": request.user.email,
            "name": request.user.get_full_name(),
            "organization_id": str(service.organization_id),
            "service_id": str(service.pk),
            "identity_name": service.organization.brand,
        }
        if membership:
            claims.update(authorization_for(membership, service))
        return claims

    def get_discovery_claims(self, request):
        return ["sub", "name", "email", "organization_id", "service_id", "identity_name", "roles", "permissions"]
