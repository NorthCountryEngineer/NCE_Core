"""Framework-independent reference client for resource services. No Django dependency.

Copy this module into a resource service that has requests installed, or import it
from this checkout. Client credentials must stay on the service's backend.
"""
import time
import os
from pathlib import Path
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
    environment: str | None = None
    deployment_id: str | None = None

    @classmethod
    def from_environment(cls):
        return cls(issuer=os.environ['CORE_ISSUER'], client_id=os.environ['CORE_OIDC_CLIENT_ID'],
            client_secret=Path(os.environ['CORE_OIDC_CLIENT_SECRET_FILE']).read_text().strip(),
            organization_id=os.environ['CORE_ORGANIZATION_ID'], service_id=os.environ['CORE_SERVICE_ID'],
            environment=os.environ.get('CORE_ENVIRONMENT'), deployment_id=os.environ.get('CORE_DEPLOYMENT_ID'),
            allow_local_http=os.environ.get('CORE_ALLOW_LOCAL_HTTP') == '1')

    def __post_init__(self):
        parsed = urlparse(self.issuer)
        if (not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or
            (parsed.scheme != "https" and not (self.allow_local_http and parsed.scheme == "http" and
                                               parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("Use an HTTPS issuer origin (HTTP loopback requires explicit development opt-in).")
        if not all([self.client_id, self.client_secret, self.organization_id, self.service_id]):
            raise ValueError("Client credentials and expected organization/service IDs are required.")
        if self.environment and self.environment not in {'sandbox', 'development', 'staging', 'uat', 'production'}:
            raise ValueError('Use a registered deployment environment.')

    def require(self, access_token: str, permission: str, purpose_code: str = "", resource_type: str = "", resource_id: str = "") -> dict:
        if not access_token or not permission:
            raise Unauthenticated("A bearer access token and permission are required.")
        try:
            response = requests.post(
                self.issuer + "/oauth/introspect/", data={"token": access_token, "purpose_code": purpose_code},
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
        if (self.environment and claims.get('environment') != self.environment) or (self.deployment_id and claims.get('deployment_id') != self.deployment_id):
            raise Unauthenticated('The token does not match this deployment environment.')
        permissions = claims.get("permissions")
        if not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions):
            raise IdentityUnavailable("Malformed authorization response.")
        if permission not in permissions:
            raise Forbidden("The user does not have the required permission.")
        if resource_type or resource_id:
            if not resource_type or not resource_id:
                raise ValueError("Both a registered resource type and ID are required.")
            try:
                response = requests.post(self.issuer + "/api/v1/authorize/", data={"token": access_token, "permission": permission,
                    "organization_id": self.organization_id, "purpose_code": purpose_code,
                    "resource_type": resource_type, "resource_id": resource_id},
                    auth=(quote(self.client_id, safe=""), quote(self.client_secret, safe="")), timeout=self.timeout, allow_redirects=False)
                if response.status_code != 200:
                    raise IdentityUnavailable("Resource authorization could not be verified.")
                value = response.json()
                if not isinstance(value, dict) or type(value.get("allowed")) is not bool:
                    raise IdentityUnavailable("Malformed resource decision.")
                if not value["allowed"]:
                    raise Forbidden("Resource access policy denied this operation.")
                if value.get("subject") != claims["sub"]:
                    raise IdentityUnavailable("Resource authorization subject mismatch.")
            except (requests.RequestException, ValueError) as error:
                raise IdentityUnavailable("Resource authorization could not be verified.") from error
        return claims
