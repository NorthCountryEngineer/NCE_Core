from oauth2_provider.oauth2_backends import OAuthLibCore
from .access import request_principal, session_evidence


class EvidenceOAuthLibCore(OAuthLibCore):
    def extract_headers(self, request):
        headers = super().extract_headers(request)
        # Always replace this internal attribute; a caller's headers cannot supply assurance.
        evidence = session_evidence(request) if request.user.is_authenticated else None
        headers["core_authentication_evidence"] = str(evidence.pk) if evidence else ""
        headers["core_purpose_code"] = request_principal(request).purpose if evidence else ""
        headers["core_request_id"] = str(getattr(request, "security_request_id", ""))
        return headers
