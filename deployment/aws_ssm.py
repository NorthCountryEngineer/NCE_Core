"""One-off registration transport using ECS task credentials, without boto3.

Never called during a build. Only the dedicated registration task has IAM
permission to write its one configured SSM parameter.
"""
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from urllib.request import Request, urlopen


def signature_headers(region, credentials, body, now=None):
    now = now or datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    host = f"ssm.{region}.amazonaws.com"
    headers = {"content-type": "application/x-amz-json-1.1", "host": host,
               "x-amz-date": timestamp, "x-amz-security-token": credentials["Token"],
               "x-amz-content-sha256": hashlib.sha256(body).hexdigest(),
               "x-amz-target": "AmazonSSM.PutParameter"}
    names = ";".join(sorted(headers))
    canonical = "\n".join(["POST", "/", "", "".join(f"{name}:{headers[name]}\n" for name in sorted(headers)),
                            names, hashlib.sha256(body).hexdigest()])
    scope = f"{day}/{region}/ssm/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", timestamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + credentials["SecretAccessKey"]).encode()
    for part in [day, region, "ssm", "aws4_request"]:
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={credentials['AccessKeyId']}/{scope}, SignedHeaders={names}, Signature={signature}"
    return headers


def put_secure_parameter(name, value):
    if name != os.environ.get("CORE_REGISTRATION_SSM_PARAMETER"):
        raise ValueError("SSM output must match the dedicated registration task's configured parameter")
    region = os.environ.get("AWS_REGION", "")
    relative = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "")
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d+", region) or not re.fullmatch(r"/v2/credentials/[A-Za-z0-9-]+", relative):
        raise ValueError("Run SSM registration only in its dedicated ECS task with task-role credentials")
    # The host is a literal ECS metadata address, never a caller-controlled URL.
    try:
        with urlopen("http://169.254.170.2" + relative, timeout=5) as response:
            credentials = json.load(response)
        body = json.dumps({"Name": name, "Value": value, "Type": "SecureString", "Tier": "Standard",
                           "Overwrite": False}, separators=(",", ":")).encode()
        headers = signature_headers(region, credentials, body)
        request = Request(f"https://ssm.{region}.amazonaws.com/", data=body, headers=headers, method="POST")
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
        return result["Version"]
    except Exception:
        # Exceptions might contain HTTP request metadata; report a stable message
        # without printing any secret, token, request body or response body.
        raise RuntimeError("Secure credential transfer failed; inspect registration IAM and parameter configuration") from None
