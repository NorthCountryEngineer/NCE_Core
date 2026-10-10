"""A bounded, declarative header catalog. Configuration never grants access."""
from copy import deepcopy
import re
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from identity.access import decision
from identity.models import PermissionTag
from .models import Deployment

WIDGETS = {'apps': 'Application launcher', 'pins': 'Shared shortcuts and personal pins',
    'links': 'Organization links', 'account': 'Account and administrative tools',
    'chat': 'Chat and notifications', 'environment': 'Current deployment environment'}
PRESETS = {'navy': ('#24263e', '#ffffff', '#6250ac'), 'slate': ('#172b40', '#ffffff', '#2469a0'),
    'forest': ('#153c31', '#ffffff', '#28775c'), 'light': ('#f3f6fa', '#182b40', '#155c99')}
DEFAULT = {'logo_url': '', 'show_organization': True, 'density': 'comfortable', 'preset': 'navy',
    'background': '#24263e', 'foreground': '#ffffff', 'accent': '#6250ac',
    'widgets': ['apps', 'pins', 'links', 'account', 'chat'], 'shortcuts': [], 'links': []}


def safe_url(value, *, logo=False):
    if not isinstance(value, str) or not value or len(value) > 2048 or re.search(r'[\s\\\x00-\x1f\x7f]', value):
        raise ValidationError('Use a complete HTTPS URL without whitespace or credentials.')
    try:
        parsed = urlsplit(value)
        URLValidator(schemes=['https', 'http'])(value)
        _ = parsed.port
    except (ValueError, ValidationError):
        raise ValidationError('Use a complete HTTPS URL.')
    local = settings.DEBUG and parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}
    if (parsed.scheme != 'https' and not local) or parsed.username is not None or parsed.password is not None:
        raise ValidationError('Use HTTPS; HTTP is allowed only for local development.')
    if logo and (parsed.query or parsed.fragment):
        raise ValidationError('Use a public logo URL without query strings or fragments.')
    return value


def luminance(color):
    values = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return sum(weight * (v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4)
        for v, weight in zip(values, (.2126, .7152, .0722)))


def contrast(a, b):
    values = sorted((luminance(a), luminance(b)))
    return (values[1] + .05) / (values[0] + .05)


def validate(value, organization):
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise ValidationError('Use the supported header settings.')
    result = deepcopy(value)
    if result['logo_url']:
        safe_url(result['logo_url'], logo=True)
    elif result['logo_url'] != '':
        raise ValidationError('Use a logo URL or leave it blank.')
    if type(result['show_organization']) is not bool or result['density'] not in ('comfortable', 'compact') or result['preset'] not in (*PRESETS, 'custom'):
        raise ValidationError('Choose a supported appearance.')
    for key in ('background', 'foreground', 'accent'):
        if not isinstance(result[key], str) or not re.fullmatch('#[0-9a-fA-F]{6}', result[key]):
            raise ValidationError('Colors must use six-digit hexadecimal notation, such as #172b40.')
        result[key] = result[key].lower()
    if contrast(result['background'], result['foreground']) < 4.5:
        raise ValidationError('Header text and background need a contrast ratio of at least 4.5:1.')
    widgets = result['widgets']
    if not isinstance(widgets, list) or len(widgets) > len(WIDGETS) or any(not isinstance(w, str) or w not in WIDGETS for w in widgets) or len(set(widgets)) != len(widgets):
        raise ValidationError('Choose each supported widget at most once.')
    shortcuts = result['shortcuts']
    if not isinstance(shortcuts, list) or len(shortcuts) > 12:
        raise ValidationError('Choose at most twelve shared application shortcuts.')
    try:
        ids = [str(UUID(v)) for v in shortcuts]
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Choose application deployment IDs.')
    if len(set(ids)) != len(ids) or Deployment.objects.filter(pk__in=ids, installation__organization=organization).count() != len(ids):
        raise ValidationError('Shared applications must belong to this organization.')
    result['shortcuts'] = ids
    links = result['links']
    if not isinstance(links, list) or len(links) > 8:
        raise ValidationError('Configure at most eight organization links.')
    for link in links:
        if not isinstance(link, dict) or set(link) != {'label', 'url', 'new_tab', 'permission_tags'}:
            raise ValidationError('Use a label, URL, tab preference, and permission tags for each link.')
        if not isinstance(link['label'], str) or not 1 <= len(link['label'].strip()) <= 60 or re.search(r'[\x00-\x1f\x7f]', link['label']):
            raise ValidationError('Link labels need 1–60 characters.')
        link['label'] = link['label'].strip()
        safe_url(link['url'])
        tags = link['permission_tags']
        if type(link['new_tab']) is not bool or not isinstance(tags, list) or len(tags) > 20 or any(type(pk) is not int for pk in tags) or len(set(tags)) != len(tags):
            raise ValidationError('Use distinct organization permission tags and a boolean tab preference.')
        if PermissionTag.objects.filter(pk__in=tags, organization=organization, active=True).count() != len(tags):
            raise ValidationError('Link permission tags must be active and belong to this organization.')
    return result


def configuration(organization):
    # Older installations and invalid direct database writes have safe defaults.
    if not organization.header_configuration:
        return deepcopy(DEFAULT)
    try:
        return validate(organization.header_configuration, organization)
    except (ValidationError, TypeError, ValueError):
        return deepcopy(DEFAULT)


def presentation(organization, principal, rows, source=None):
    config = configuration(organization)
    by_id = {row['id']: row for row in rows}
    shortcuts = [by_id[pk] for pk in config.pop('shortcuts') if pk in by_id]
    links = []
    for link in config.pop('links'):
        tags = PermissionTag.objects.filter(pk__in=link['permission_tags'], organization=organization, active=True)
        if all(decision(principal, tag)[0] and (source is None or decision(source, tag)[0]) for tag in tags):
            links.append({key: link[key] for key in ('label', 'url', 'new_tab')})
    config['accent_foreground'] = '#ffffff' if contrast(config['accent'], '#ffffff') >= 4.5 else '#000000'
    return config, shortcuts, links
