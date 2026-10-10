import json
from pathlib import Path
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError


class FabricUnavailable(Exception):
    pass


def validate_origin(issuer):
    try:
        url = urlparse(issuer)
        url.port
    except (ValueError, TypeError):
        raise ValidationError('Use a valid peer origin.')
    local = settings.DEBUG and url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'}
    if (not url.netloc or url.username is not None or url.path or url.query or url.fragment or
            not (local or url.scheme == 'https' and (issuer == settings.ISSUER or issuer in settings.CORE_FABRIC_ORIGINS))):
        raise ValidationError('Use an exact HTTPS origin approved in CORE_FABRIC_ORIGINS.')
    return issuer


def credential_path(peer):
    root = Path(settings.CORE_FABRIC_SECRET_ROOT).resolve() / str(peer.organization_id)
    path = Path(peer.credential_file)
    if not path.is_absolute() or not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise ValidationError('Mount credentials under CORE_FABRIC_SECRET_ROOT/<organization-uuid>/.')
    return path.resolve()


def validate_peer(peer):
    validate_origin(peer.issuer)
    if peer.issuer == settings.ISSUER:
        raise ValidationError('Use a different Core deployment as the peer.')
    credential_path(peer)


def request_peer(peer, suffix, values):
    validate_peer(peer)
    if not peer.active:
        raise PermissionDenied('Peer is disabled.')
    try:
        token = credential_path(peer).read_text().strip()
        if not token or len(token) > 256 or any(c.isspace() for c in token):
            raise ValueError
        with requests.post(peer.issuer + '/api/platform/v1/fabric/' + suffix, json=values,
                headers={'Authorization': 'Bearer ' + token, 'X-Core-Fabric-Issuer': settings.ISSUER,
                         'X-Core-Purpose': values.get('purpose_code', '')},
                timeout=(3, 10), allow_redirects=False, stream=True) as reply:
            if reply.status_code in {401, 403, 404}:
                raise PermissionDenied('Peer approval or permission was denied.')
            if reply.status_code == 409:
                from platform_services.data import IdempotencyConflict
                raise IdempotencyConflict('Federated request ID conflict.')
            if reply.status_code not in {200, 201}:
                raise ValueError
            chunks, length = [], 0
            for chunk in reply.iter_content(65536):
                length += len(chunk)
                if length > 4 * 1024 * 1024:
                    raise ValueError
                chunks.append(chunk)
            value = json.loads(b''.join(chunks), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if (not isinstance(value, dict) or value.get('issuer') != peer.issuer or
                    value.get('organization_id') != str(peer.remote_organization_id)):
                raise ValueError
            return value
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError, RecursionError):
        raise FabricUnavailable('Peer unavailable or returned an invalid federation response.') from None
