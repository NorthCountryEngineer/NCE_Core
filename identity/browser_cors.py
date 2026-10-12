"""Exact registered-origin CORS for secretless browser PKCE exchanges only."""
from django.http import HttpResponse, JsonResponse
from django.utils.cache import patch_vary_headers

from .models import Application


class OAuthBrowserCorsMiddleware:
    paths = {'/oauth/token/', '/oauth/revoke/'}

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def allowed(application, origin):
        return bool(application and application.client_type == Application.CLIENT_PUBLIC and application.is_usable(None) and
            origin in application.allowed_origins.split())

    def __call__(self, request):
        origin = request.headers.get('Origin')
        if request.path not in self.paths or not origin:
            return self.get_response(request)
        if request.method == 'OPTIONS':
            headers = {header.strip().lower() for header in request.headers.get('Access-Control-Request-Headers', '').split(',') if header.strip()}
            allowed = request.headers.get('Access-Control-Request-Method') == 'POST' and headers <= {'content-type'} and any(
                self.allowed(application, origin) for application in Application.objects.filter(active=True, client_type=Application.CLIENT_PUBLIC,
                    service__active=True, service__organization__active=True, allowed_origins__contains=origin))
            response = HttpResponse(status=204) if allowed else JsonResponse({'error': 'origin_not_allowed'}, status=403)
        elif request.method == 'POST':
            application = Application.objects.filter(client_id=request.POST.get('client_id')).first()
            allowed = self.allowed(application, origin) and not request.headers.get('Authorization')
            response = self.get_response(request) if allowed else JsonResponse({'error': 'origin_not_allowed'}, status=403)
        else:
            return self.get_response(request)
        response.headers.pop('Access-Control-Allow-Origin', None)
        response.headers.pop('Access-Control-Allow-Credentials', None)
        if allowed:
            response['Access-Control-Allow-Origin'] = origin
            if request.method == 'OPTIONS':
                response['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
                response['Access-Control-Allow-Headers'] = 'Content-Type'
                response['Access-Control-Max-Age'] = '0'
        response['Cache-Control'] = 'no-store'
        response['Pragma'] = 'no-cache'
        patch_vary_headers(response, ['Origin'])
        return response
