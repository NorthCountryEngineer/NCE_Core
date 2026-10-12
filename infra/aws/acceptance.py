#!/usr/bin/env python3
"""Post-deployment HTTP gate. Makes requests only after an explicit cost opt-in."""
import argparse
import os
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests


class CSRFParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "input" and attributes.get("name") == "csrfmiddlewaretoken":
            self.token = attributes.get("value")


def origin(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise argparse.ArgumentTypeError("Use an HTTPS origin without a path or credentials")
    return value.rstrip("/")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(public, backend, browser, client_id):
    session = requests.Session()
    discovery = session.get(public + "/.well-known/openid-configuration", timeout=30)
    require(discovery.status_code == 200, "Public discovery failed")
    document = discovery.json()
    require(document.get("issuer") == public, "Core issuer does not match its permanent Amplify origin")
    for key in ["authorization_endpoint", "token_endpoint", "jwks_uri"]:
        require(document.get(key, "").startswith(public + "/"), f"Discovery {key} points outside the public origin")
    denied = session.get(backend + "/.well-known/openid-configuration", timeout=30, allow_redirects=False)
    require(denied.status_code == 403, "ALB origin bypass was not rejected")
    login_url = public + "/accounts/login/"
    login = session.get(login_url, timeout=30)
    require(login.status_code == 200, "Login page unavailable")
    require("no-store" in login.headers.get("Cache-Control", ""), "Login page permits shared caching")
    csrf = session.cookies.get("csrftoken")
    require(csrf, "Amplify did not preserve the CSRF cookie")
    require(any(cookie.name == "csrftoken" and cookie.secure for cookie in session.cookies), "CSRF cookie is not Secure")
    parser = CSRFParser()
    parser.feed(login.text)
    require(parser.token, "Login form has no CSRF token")
    # An intentionally nonexistent account checks a normal form POST without
    # changing a real account or storing any user password in this script.
    form = {"username": "aws-acceptance-invalid@example.invalid", "password": "InvalidAcceptancePassword-Only",
            "csrfmiddlewaretoken": parser.token}
    valid = session.post(login_url, data=form, headers={"Origin": public, "Referer": login_url}, timeout=30, allow_redirects=False)
    require(valid.status_code == 200, "Cookie/form POST failed at the proxy or CSRF boundary")
    invalid = session.post(login_url, data=form, headers={"Origin": "https://unregistered.example", "Referer": login_url}, timeout=30, allow_redirects=False)
    require(invalid.status_code == 403, "Cross-origin login POST was not rejected by CSRF")
    no_csrf = session.post(login_url, data={"username": form["username"], "password": form["password"]},
                           headers={"Origin": public}, timeout=30, allow_redirects=False)
    require(no_csrf.status_code == 403, "Login POST without a CSRF token was accepted")
    options = {"Origin": browser, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    preflight = session.options(public + "/oauth/token/", headers=options, timeout=30)
    require(preflight.status_code == 204 and preflight.headers.get("Access-Control-Allow-Origin") == browser,
            "Registered browser origin OPTIONS was not preserved")
    token = session.post(public + "/oauth/token/", headers={"Origin": browser}, data={"client_id": client_id,
        "grant_type": "authorization_code", "code": "invalid-acceptance-code", "code_verifier": "x" * 64}, timeout=30)
    require(token.status_code == 400 and token.headers.get("Access-Control-Allow-Origin") == browser,
            "Browser token POST/CORS transport failed")
    options["Origin"] = "https://unregistered.example"
    rejected = session.options(public + "/oauth/token/", headers=options, timeout=30)
    require(rejected.status_code == 403 and "Access-Control-Allow-Origin" not in rejected.headers,
            "Unregistered browser origin was allowed")
    print("Core HTTPS, issuer, origin boundary, cookies, CSRF and OAuth POST/OPTIONS checks passed.")
    print("Complete a real administrator login and Axolotl PKCE sign-in before inviting users.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-origin", required=True, type=origin)
    parser.add_argument("--backend-origin", required=True, type=origin)
    parser.add_argument("--browser-origin", default="https://axolotl.news", type=origin)
    parser.add_argument("--browser-client-id", required=True)
    args = parser.parse_args()
    if os.environ.get("CORE_ENABLE_AWS_DEPLOYMENT") != "true":
        parser.error("Requests use deployed AWS compute. Set CORE_ENABLE_AWS_DEPLOYMENT=true only after reviewing costs.")
    try:
        run(args.public_origin, args.backend_origin, args.browser_origin, args.browser_client_id)
    except (RuntimeError, requests.RequestException, ValueError) as error:
        raise SystemExit(f"Deployment acceptance failed: {error}") from None
