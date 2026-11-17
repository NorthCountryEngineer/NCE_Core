"""Exact universal-link association for the native robotics public client."""
import re
from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def association(request):
    team = getattr(settings, 'CORE_IOS_TEAM_ID', '')
    bundle = getattr(settings, 'CORE_IOS_BUNDLE_ID', '')
    if not re.fullmatch(r'[A-Z0-9]{10}', team) or not re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', bundle):
        raise Http404('Native app association is not configured.')
    # ASWebAuthenticationSession's HTTPS callback validates this same exact app
    # association through webcredentials. The app does not call password-sharing
    # APIs; no wildcard apps or domains are granted here.
    return JsonResponse({'applinks': {'details': [{'appIDs': [team + '.' + bundle],
        'components': [{'/': '/apps/nce-robotics/oauth/callback', 'comment': 'NCE Robotics OAuth callback'}]}]},
        'webcredentials': {'apps': [team + '.' + bundle]}})


@require_GET
def callback(request):
    # Never echo the authorization code, state, or query string into HTML.
    result = HttpResponse('<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>NCE Robotics sign-in</title><body><h1>Return to NCE Robotics</h1>'
        '<p>Complete sign-in in the NCE Robotics iOS application.</p></body></html>', content_type='text/html')
    result['Cache-Control'] = 'no-store'
    result['Referrer-Policy'] = 'no-referrer'
    result['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'"
    return result
