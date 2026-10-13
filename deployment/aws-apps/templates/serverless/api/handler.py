"""Core-protected serverless starter. Runtime credentials stay in Parameter Store."""
import base64
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

import boto3
from botocore.config import Config


_session = boto3.Session()
_ssm = _session.client("ssm", config=Config(connect_timeout=3, read_timeout=5,
    retries={"mode": "standard", "total_max_attempts": 2}))
_dynamo = _session.resource("dynamodb")


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise APIError(503, "Core authentication is temporarily unavailable.")


def request_fields(event):
    """Support both documented HTTP API Lambda proxy payload formats."""
    method = (event.get("requestContext") or {}).get("http", {}).get("method") or event.get("httpMethod", "")
    path = event.get("rawPath") or event.get("path", "/")
    headers = {str(name).lower(): value for name, value in (event.get("headers") or {}).items()}
    return method, path, headers


def authorize(event, permission):
    header = request_fields(event)[2].get("authorization", "")
    if not isinstance(header, str) or not header.startswith("Bearer ") or not 1 <= len(header[7:]) <= 4096:
        raise APIError(401, "Sign in to continue.")
    issuer = os.environ.get("CORE_ISSUER", "")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+", issuer):
        raise APIError(503, "Core authentication has not been configured.")
    try:
        credentials = json.loads(_ssm.get_parameter(Name=os.environ["CORE_CREDENTIALS_PARAMETER"], WithDecryption=True)["Parameter"]["Value"])
    except Exception:
        raise APIError(503, "Core authentication is temporarily unavailable.") from None
    basic = base64.b64encode((urlencode({"v": credentials["client_id"]})[2:] + ":" +
                             urlencode({"v": credentials["client_secret"]})[2:]).encode()).decode()
    request = Request(issuer + "/oauth/introspect/", data=urlencode({"token": header[7:]}).encode(),
        headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with build_opener(NoRedirect).open(request, timeout=5) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise APIError(503, "Core authentication returned an invalid response.")
            claims = json.loads(raw)
    except (HTTPError, URLError, TimeoutError, ValueError):
        raise APIError(503, "Core authentication is temporarily unavailable.") from None
    expected = {"iss": issuer, "organization_id": os.environ["CORE_ORGANIZATION_ID"],
        "service_id": os.environ["CORE_SERVICE_ID"], "aud": "urn:core:service:" + os.environ["CORE_SERVICE_ID"]}
    if (not isinstance(claims, dict) or claims.get("active") is not True
            or any(claims.get(key) != value for key, value in expected.items())
            or not isinstance(claims.get("exp"), (int, float)) or isinstance(claims.get("exp"), bool)
            or claims["exp"] <= time.time() or not isinstance(claims.get("sub"), str)
            or not re.fullmatch(r"[a-f0-9-]{36}", claims["sub"])
            or "authorization" not in str(claims.get("scope", "")).split()
            or str(claims.get("token_type", "")).lower() != "bearer"):
        raise APIError(401, "This Core session is inactive or belongs to another application.")
    permissions = claims.get("permissions", [])
    if not isinstance(permissions, list) or permission not in permissions:
        raise APIError(403, "Your Core account does not have this application permission.")
    return claims


def dispatch(event):
    method, path, _ = request_fields(event)
    if method == "GET" and path == "/health":
        return {"status": "ok", "application": os.environ["NCE_APP_ID"]}
    if method == "GET" and path == "/session":
        claims = authorize(event, "app-read")
        return {"subject": claims["sub"], "permissions": claims["permissions"]}
    if path == "/notes" and method in {"GET", "POST"}:
        claims = authorize(event, "app-read" if method == "GET" else "app-write")
        table = _dynamo.Table(os.environ["PRIVATE_TABLE"])
        if method == "GET":
            item = table.get_item(Key={"id": claims["sub"]}, ConsistentRead=True).get("Item", {})
            return {"note": item.get("note", "")}
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw, validate=True).decode()
        if len(raw) > 8192:
            raise APIError(400, "Note is too large.")
        value = json.loads(raw)
        note = value.get("note") if isinstance(value, dict) else None
        if not isinstance(note, str) or len(note) > 2000:
            raise APIError(400, "A note must contain at most 2000 characters.")
        table.put_item(Item={"id": claims["sub"], "note": note})
        return {"note": note}
    raise APIError(404, "This endpoint does not exist.")


def handler(event, context):
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    method, _, incoming_headers = request_fields(event)
    origin = incoming_headers.get("origin", "")
    if origin == os.environ.get("APPLICATION_ORIGIN"):
        headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin",
            "Access-Control-Allow-Headers": "authorization,content-type", "Access-Control-Allow-Methods": "GET,POST,OPTIONS"})
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": headers, "body": ""}
    try:
        data, status = dispatch(event), 200
    except APIError as error:
        data, status = {"error": error.message}, error.status
    except (ValueError, KeyError, TypeError):
        data, status = {"error": "Invalid request."}, 400
    except Exception:
        data, status = {"error": "The application is temporarily unavailable."}, 503
    return {"statusCode": status, "headers": headers, "body": json.dumps(data)}
