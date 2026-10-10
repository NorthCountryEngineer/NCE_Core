import hashlib
from oauth2_provider.models import get_access_token_model
from identity.views import token_claims
from platform_services.api import body, response
from platform_services.integration_api import integration_api
from .services import catalog, query


@integration_api(namespace='ai', atomic=False)
def sources(request):
    return response(catalog(request.access_principal, request.organization, request.integration_source))


@integration_api(('POST',), namespace='ai', atomic=False)
def unified_query(request):
    def current_delegation():
        raw = request.headers.get('X-Core-User-Token', '')
        token = get_access_token_model().objects.select_related('application__service', 'user').filter(
            token_checksum=hashlib.sha256(raw.encode()).hexdigest()).first()
        claims = token_claims(raw, token.application, request.access_principal.purpose) if token and token.application else None
        return bool(claims and claims.get('active') and 'authorization' in token.scopes and
            token.application_id == request.access_principal.application.pk and token.user_id == request.access_principal.user.pk)
    return response(query(request.access_principal, request.organization, body(request), request.integration_source,
        revalidate=current_delegation))
