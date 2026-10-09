from django.conf import settings
from .models import Application, Organization


def context(request):
    organization = getattr(request, "organization", None)
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
            "brand_organization": organization}
