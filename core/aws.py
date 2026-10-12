"""The AWS origin boundary; enabled explicitly by the Fargate deployment."""
from hmac import compare_digest
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import FileResponse, HttpResponse, JsonResponse


class AWSOriginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.secret = settings.CORE_ORIGIN_SECRET

    def __call__(self, request):
        # ALB health checks have no customer Host or origin credential. Return only
        # a database readiness bit, before SSL redirection and host validation.
        if request.path == "/health/" and request.method in {"GET", "HEAD"}:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
            except Exception:
                return HttpResponse(status=503)
            return JsonResponse({"status": "ok"})
        supplied = request.META.get("HTTP_X_CORE_ORIGIN_SECRET", "")
        if not compare_digest(supplied.encode(), self.secret.encode()):
            return HttpResponse(status=403)
        # Django checks its public Host and CSRF Origin normally. The trusted ALB
        # sets X-Forwarded-Proto based on its HTTPS listener.
        if request.path.startswith(settings.STATIC_URL):
            root = Path(settings.STATIC_ROOT).resolve()
            path = (root / request.path[len(settings.STATIC_URL):]).resolve()
            if root not in path.parents or not path.is_file():
                return HttpResponse(status=404)
            response = FileResponse(path.open("rb"))
            response["Cache-Control"] = "public, max-age=3600"
            response["X-Content-Type-Options"] = "nosniff"
            return response
        response = self.get_response(request)
        # Authenticated pages, OAuth responses and personalized API responses must
        # never be cached by Amplify/CloudFront or a shared intermediary.
        response["Cache-Control"] = "private, no-store"
        return response
