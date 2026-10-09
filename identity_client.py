"""Framework-independent reference client for resource services. No Django dependency.

Copy this module into a resource service that has requests installed, or import it
from this checkout. Client credentials must stay on the service's backend.
"""
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import requests


class Unauthenticated(Exception):
    """The presented access token is invalid or not intended for this service."""


class Forbidden(Exception):
    """The authenticated user lacks the requested permission."""


class IdentityUnavailable(Exception):
    """Central authorization could not be verified. Deny the request."""


@dataclass(frozen=True)
class ResourceClient:
    issuer: str
    client_id: str
    client_secret: str
    organization_id: str
    service_id: str
    timeout: float = 5.0
    allow_local_http: bool = False

    def __post_init__(self):
        parsed = urlparse(self.issuer)
        if (not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or
            (parsed.scheme != "https" and not (self.allow_local_http and parsed.scheme == "http" and
                                               parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("Use an HTTPS issuer origin (HTTP loopback requires explicit development opt-in).")
        if not all([self.client_id, self.client_secret, self.organization_id, self.service_id]):
            raise ValueError("Client credentials and expected organization/service IDs are required.")

    def require(self, access_token: str, permission: str) -> dict:
        if not access_token or not permission:
            raise Unauthenticated("A bearer access token and permission are required.")
        try:
            response = requests.post(
                self.issuer + "/oauth/introspect/", data={"token": access_token},
                # OAuth client authentication uses form-encoded credentials in Basic auth.
                auth=(quote(self.client_id, safe=""), quote(self.client_secret, safe="")),
                timeout=self.timeout, allow_redirects=False,
            )
            if response.status_code != 200:
                raise IdentityUnavailable("The identity service did not accept the authorization check.")
            claims = response.json()
        except (requests.RequestException, ValueError) as error:
            raise IdentityUnavailable("Unable to verify authorization with the identity service.") from error
        if not isinstance(claims, dict) or claims.get("active") is not True:
            raise Unauthenticated("The access token is inactive.")
        if (claims.get("iss") != self.issuer or claims.get("organization_id") != self.organization_id or
                claims.get("service_id") != self.service_id or
                claims.get("aud") != f"urn:core:service:{self.service_id}" or
                not isinstance(claims.get("sub"), str) or not claims["sub"] or
                type(claims.get("exp")) is not int or claims["exp"] <= time.time()):
            raise Unauthenticated("The token does not match this service and organization.")
        permissions = claims.get("permissions")
        if not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions):
            raise IdentityUnavailable("Malformed authorization response.")
        if permission not in permissions:
            raise Forbidden("The user does not have the required permission.")
        return claims
