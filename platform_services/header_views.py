"""Same-origin browser endpoints and a token-free portable component asset."""
from pathlib import Path
from functools import wraps

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie

from core_chat.federation import FederationUnavailable
from core_chat.portal import member
from identity.access import request_principal
from identity.models import Organization
from identity.views import audit, organization_admin
from . import header
from .api import body, response
from .data import IdempotencyConflict
from .integration_api import integration_api


def dispatch(request, operation, pk=None, *, source=None):
    principal, organization = request.access_principal, request.organization
    header.context(principal, organization, source)
    installation = source.credential.installation if source else None
    if operation == 'state':
        return response(header.state(principal, organization, source))
    if not header.enabled(organization, installation):
        raise PermissionDenied('Shared header is disabled.')
    if operation == 'pins':
        header.pin(principal, organization, body(request))
        return response(header.state(principal, organization, source))
    if operation == 'messages':
        if set(request.GET) - {'after'}:
            raise ValidationError('Only a read cursor is supported.')
        return response(header.chat_messages(principal, organization, pk, request.GET.get('after'), source))
    if operation == 'send':
        value = header.chat_send(principal, organization, pk, body(request), source)
        return response(value, 201 if value['created'] else 200)
    if operation == 'preferences':
        return response(header.chat_preferences(principal, organization, pk, body(request), source))
    raise ValidationError('Unknown user-tool operation.')


def browser(methods):
    def decorate(view):
        @member
        @require_http_methods(methods)
        @wraps(view)
        def wrapped(request, organization, *args, **kwargs):
            request.access_principal = request_principal(request)
            try:
                return view(request, *args, **kwargs)
            except ValidationError:
                return response({'error': 'invalid_request'}, 400)
            except PermissionDenied:
                return response({'error': 'insufficient_permission'}, 403)
            except IdempotencyConflict:
                return response({'error': 'idempotency_conflict'}, 409)
            except FederationUnavailable:
                return response({'error': 'chat_authority_unavailable'}, 503)
        return wrapped
    return decorate


@browser(('GET',))
@ensure_csrf_cookie
def state(request):
    return dispatch(request, 'state')


@browser(('POST',))
def pins(request):
    return dispatch(request, 'pins')


@browser(('GET',))
def messages(request, pk):
    return dispatch(request, 'messages', pk)


@browser(('POST',))
def send(request, pk):
    return dispatch(request, 'send', pk)


@browser(('POST',))
def preferences(request, pk):
    return dispatch(request, 'preferences', pk)


def api(methods, operation):
    @integration_api(methods, namespace='header')
    def endpoint(request, pk=None):
        try:
            return dispatch(request, operation, pk, source=request.integration_source)
        except IdempotencyConflict:
            return response({'error': 'idempotency_conflict'}, 409)
        except FederationUnavailable:
            return response({'error': 'chat_authority_unavailable'}, 503)
    return endpoint


api_state = api(('GET',), 'state')
api_pins = api(('POST',), 'pins')
api_messages = api(('GET',), 'messages')
api_send = api(('POST',), 'send')
api_preferences = api(('POST',), 'preferences')


@require_GET
def component(request):
    result = FileResponse((Path(__file__).parent / 'assets' / 'header.js').open('rb'), content_type='text/javascript; charset=utf-8')
    result['Cache-Control'] = 'public, max-age=300'
    result['X-Content-Type-Options'] = 'nosniff'
    return result


@organization_admin
def settings(request, organization):
    class HeaderForm(forms.ModelForm):
        class Meta:
            model = Organization
            fields = ['unified_header_enabled']
            labels = {'unified_header_enabled': 'Show the shared header by default'}
            help_texts = {'unified_header_enabled': 'Includes app pins, account tools, Chat, and unread notifications. Each installed app can inherit this setting or override it in Set service access. Applications include the header component and backend adapter described in docs/unified-header.md.'}
    form = HeaderForm(request.POST or None, instance=organization)
    if request.method == 'POST' and form.is_valid():
        form.save()
        audit(request, 'header.configuration_updated', organization.pk)
        return redirect('platform_dashboard', slug=organization.slug)
    return render(request, 'identity/form.html', {'form': form, 'title': 'Shared application header'})
