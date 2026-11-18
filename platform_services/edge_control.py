"""Short-lived authority for direct LAN control; Core is outside the packet path."""
import hashlib
import ipaddress
import json
import time
from functools import lru_cache
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from django.conf import settings
from django.core.exceptions import ValidationError

from . import edge

PROTOCOL = 'nce-local-v1'
AUDIENCE = 'nce-robotics-local-control'
TICKET_SECONDS = 30


@lru_cache(maxsize=2)
def signer(pem):
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    public = key.public_key()
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    kid = 'nce-control-' + hashlib.sha256(der).hexdigest()[:24]
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public))
    jwk.update(kid=kid, alg='RS256', use='sig')
    return key, kid, jwk


def trust_configuration(device):
    """Only the enrolled agent can retrieve and publish this public trust document."""
    _, _, key = signer(settings.OIDC_PRIVATE_KEY)
    return {'issuer': settings.ISSUER, 'device_id': str(device.pk),
            'expires_at': int(time.time()) + 75, 'jwks': {'keys': [key]}}


def local_descriptor(device):
    descriptor = device.report.get('local_control')
    if (not isinstance(descriptor, dict) or descriptor.get('protocol') != PROTOCOL
            or descriptor.get('device_id') != str(device.pk) or descriptor.get('available') is not True
            or descriptor.get('tls_port') != 8767 or descriptor.get('udp_port') != 8768):
        raise ValidationError('The device has not reported a ready local control service.')
    fingerprint = descriptor.get('certificate_sha256')
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
        raise ValidationError('The device certificate identity is unavailable.')
    addresses = descriptor.get('addresses')
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 16:
        raise ValidationError('The device has no local network address.')
    allowed = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    for address in addresses:
        try:
            parsed = ipaddress.IPv4Address(address)
        except (ValueError, TypeError):
            raise ValidationError('Use an advertised private IPv4 address.') from None
        if not any(parsed in network for network in allowed):
            raise ValidationError('Direct control is restricted to the local network.')
    return {key: descriptor[key] for key in ('protocol', 'device_id', 'certificate_sha256',
        'tls_port', 'udp_port', 'addresses')} | {'hardware_control_available': False}


def issue_ticket(device, principal, session_id):
    edge.authorize(principal, device.installation, 'device-control', device)
    if not device.active or not device.online:
        raise ValidationError('The robot must be online to authorize a direct connection.')
    try:
        session_id = str(UUID(str(session_id)))
    except (ValueError, TypeError):
        raise ValidationError('Use a UUID control session identifier.') from None
    descriptor = local_descriptor(device)
    now = int(time.time())
    claims = {'iss': settings.ISSUER, 'aud': AUDIENCE, 'sub': str(principal.user.pk),
              'device_id': str(device.pk), 'organization_id': str(device.organization_id),
              'sid': session_id, 'jti': str(uuid4()), 'iat': now, 'nbf': now - 1,
              'exp': now + TICKET_SECONDS, 'certificate_sha256': descriptor['certificate_sha256'],
              'actions': ['diagnostics', 'stop']}
    key, kid, _ = signer(settings.OIDC_PRIVATE_KEY)
    ticket = jwt.encode(claims, key, algorithm='RS256', headers={'kid': kid, 'typ': 'nce-control+jwt'})
    edge.audit(device.installation, 'edge.local_control_authorized', principal=principal,
               device=device, target=session_id, reason='short_lived_diagnostics_and_stop')
    return {'ticket': ticket, 'expires_at': claims['exp'], 'session_id': session_id,
            'descriptor': descriptor, 'renew_after_seconds': 10}
