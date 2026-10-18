from django.conf import settings
from .models import Application, Organization


def context(request):
    from .app_auth import request_auth_context
    app_auth = request_auth_context(request)
    organization = getattr(request, "organization", None)
    if organization is None and app_auth:
        organization = app_auth["application"].service.organization
    header_organization = None
    if request.user.is_authenticated:
        from .models import Membership
        memberships = Membership.objects.filter(user=request.user, active=True, organization__active=True)
        if organization and memberships.filter(organization=organization).exists():
            request.session['header_organization'] = str(organization.pk)
            header_organization = organization
        elif getattr(request, 'resolver_match', None) and request.resolver_match.url_name in {'home', 'password_change', 'profile'}:
            selected = memberships.filter(organization_id=request.session.get('header_organization')).select_related('organization').first() if request.session.get('header_organization') else None
            if selected is None and memberships.count() == 1:
                selected = memberships.select_related('organization').first()
            header_organization = selected.organization if selected else None
    if organization is None:
        # Branding is cosmetic only; tenant authority comes from the registered client.
        client_id = request.GET.get("client_id")
        if not client_id and request.GET.get("next", "").startswith("/oauth/authorize/"):
            from urllib.parse import parse_qs, urlparse
            client_id = parse_qs(urlparse(request.GET["next"]).query).get("client_id", [None])[0]
        if client_id:
            client = Application.objects.select_related("service__organization").filter(client_id=client_id).first()
            if client:
                organization = client.service.organization
    return {"product_name": organization.brand if organization else settings.PRODUCT_NAME,
            "app_auth": app_auth,
            "auth_base_template": "identity/app_auth_base.html" if app_auth else "identity/base.html",
            'header_organization': header_organization if header_organization and header_organization.unified_header_enabled else None,
            "brand_organization": organization,
            "access_purpose": request.session.get("access_purposes", {}).get(str(organization.pk), "") if organization else ""}
