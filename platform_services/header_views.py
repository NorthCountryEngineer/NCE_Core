"""Same-origin browser endpoints and a token-free portable component asset."""
from pathlib import Path
from functools import wraps

from django.contrib import messages as notices
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie

from core_chat.federation import FederationUnavailable
from core_chat.portal import member
from identity.access import request_principal
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
    if operation in {'messages', 'send', 'preferences'}:
        from .header_configuration import configuration
        if 'chat' not in configuration(organization)['widgets']:
            raise PermissionDenied('Header Chat widget is disabled.')
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


@require_GET
def editor_component(request):
    result = FileResponse((Path(__file__).parent / 'assets' / 'header-editor.js').open('rb'), content_type='text/javascript; charset=utf-8')
    result['Cache-Control'] = 'public, max-age=300'
    result['X-Content-Type-Options'] = 'nosniff'
    return result


@organization_admin
@require_http_methods(('GET', 'POST'))
def settings(request, organization):
    from .header_forms import HeaderForm, LinkFormSet
    from .header_configuration import configuration, DEFAULT, PRESETS, WIDGETS
    current = configuration(organization)
    form = HeaderForm(request.POST if request.method == 'POST' else None, organization=organization)
    links = LinkFormSet(request.POST if request.method == 'POST' else None, prefix='links', initial=current['links'], form_kwargs={'organization': organization})
    if request.method == 'POST':
        if request.POST.get('action') == 'reset':
            organization.header_configuration = {}
            organization.save(update_fields=['header_configuration'])
            audit(request, 'header.configuration_reset', organization.pk)
            notices.success(request, 'Header appearance, widgets, and shared links restored to defaults. Personal pins and access permissions are preserved.')
            return redirect('header_settings', slug=organization.slug)
        valid_form, valid_links = form.is_valid(), links.is_valid()
        if valid_form and valid_links:
            try:
                config = form.configuration([{**{key: item.cleaned_data[key] for key in ('label', 'url', 'new_tab')},
                    'permission_tags': [tag.pk for tag in item.cleaned_data['permission_tags']]} for item in links.ordered_forms if item.cleaned_data])
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                organization.unified_header_enabled = form.cleaned_data['unified_header_enabled']
                organization.identity_name = form.cleaned_data['identity_name']
                organization.header_configuration = config
                organization.save(update_fields=['unified_header_enabled', 'identity_name', 'header_configuration'])
                audit(request, 'header.configuration_updated', organization.pk)
                notices.success(request, 'Shared header saved. Integrated applications receive it on their next refresh.')
                return redirect('header_settings', slug=organization.slug)
    try:
        preview = header.state(request_principal(request), organization)
    except PermissionDenied:
        preview = {'version': 1, 'organization_id': str(organization.pk), 'enabled': True, 'brand': organization.brand,
            'organization_name': organization.name, 'user': {'name': request.user.email}, 'applications': [], 'pins': [],
            'tools': [], 'chat': {'enabled': False, 'rooms': [], 'unread': 0, 'notifications': 0}}
    return render(request, 'platform_services/header_settings.html', {'form': form, 'links': links, 'organization': organization,
        'widgets': [(key, form['widget_' + key], form['order_' + key]) for key in WIDGETS],
        'preview_state': preview, 'header_presets': PRESETS, 'header_defaults': DEFAULT})
