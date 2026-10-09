"""Run from the repository root: python -m examples.protected_service.

An HTTP resource-service example. A real application supplies its own OIDC login
and passes the resulting access token to these routes.
"""
import json
import os
from wsgiref.simple_server import make_server

from identity_client import Forbidden, IdentityUnavailable, ResourceClient, Unauthenticated


def configured_client():
    return ResourceClient(
        issuer=os.environ["IDENTITY_ISSUER"], client_id=os.environ["SERVICE_CLIENT_ID"],
        client_secret=os.environ["SERVICE_CLIENT_SECRET"], organization_id=os.environ["ORGANIZATION_ID"],
        service_id=os.environ["SERVICE_ID"], allow_local_http=os.environ.get("ALLOW_LOCAL_HTTP") == "1",
    )


def make_application(client):
    def application(environ, start_response):
        permission = {"/reports": "read", "/admin": "admin"}.get(environ.get("PATH_INFO"))
        if environ.get("REQUEST_METHOD") != "GET":
            status, body = "405 Method Not Allowed", {"error": "method_not_allowed"}
        elif not permission:
            status, body = "404 Not Found", {"error": "not_found"}
        else:
            authorization = environ.get("HTTP_AUTHORIZATION", "")
            scheme, _, raw_token = authorization.partition(" ")
            try:
                if scheme.lower() != "bearer":
                    raise Unauthenticated()
                claims = client.require(raw_token, permission)
                status, body = "200 OK", {"subject": claims["sub"], "organization_id": claims["organization_id"],
                                           "message": "Service administration granted" if permission == "admin" else "Reports access granted"}
            except Unauthenticated:
                status, body = "401 Unauthorized", {"error": "invalid_token"}
            except Forbidden:
                status, body = "403 Forbidden", {"error": "insufficient_permission"}
            except IdentityUnavailable:
                status, body = "503 Service Unavailable", {"error": "authorization_unavailable"}
        encoded = json.dumps(body).encode()
        headers = [("Content-Type", "application/json"), ("Content-Length", str(len(encoded))), ("Cache-Control", "no-store")]
        if status.startswith("401"):
            headers.append(("WWW-Authenticate", "Bearer"))
        start_response(status, headers)
        return [encoded]
    return application


if __name__ == "__main__":
    with make_server("127.0.0.1", 9000, make_application(configured_client())) as server:
        server.serve_forever()
