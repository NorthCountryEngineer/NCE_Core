# Integrating a service

[Core Data Fabric](data-fabric.md) lets applications and approved agents consume
selected source-owned data across independent Core deployments. Discover `fabric_url`
and use the portable client's `fabric_records` / `fabric_publish` methods under
a contract approved by administrators on both deployments.

Core Platform provides the identity service described below. Marketplace
deployments automatically create the service/client registrations and inject the
trusted issuer, organization, service, and client settings. Applications still
implement the OIDC callback and enforce permissions using an OIDC library and the
central authorization contract. [Core APIs and automatic configuration](platform.md#application-integration)
also supply data, agents, workflows, and [Core Chat](chat.md) to application backends.
Agents use [their own accounts and live service authorization](agent-accounts.md),
with separate application/environment approvals and UAT task scopes.
[Core Training](training.md) adds provider-neutral AI simulation tools, synthetic
data consumed through the normal pipeline API, and exportable evaluation episodes.

## Register the tenant and service

An organization administrator registers a service and its permission vocabulary
in Core Identity. Record the organization's UUID and the service UUID. A service
audience is the stable string `urn:core:service:<service-uuid>`; display names and
slugs do not establish authority.

Register each OIDC application in this service with exact redirect URLs. Use a
confidential client for a server application; use a public client for a native or
browser application that cannot keep a secret. Every registration uses the code
flow, RS256 ID tokens, and mandatory PKCE S256. Production redirect URLs require
HTTPS. Explicitly enabled local development also accepts HTTP loopback URLs.

For a public frontend, register a confidential backend client in the **same
service** for introspection. It can inspect that service's tokens, including
tokens issued to other registrations of the same service. It cannot inspect
tokens issued for another service, even within the same organization.

## OIDC sign-in

Configure a maintained OIDC client library with the exact, trusted issuer origin
and its discovery document at `/.well-known/openid-configuration`. For example,
the issuer might be `https://identity.customer.example` regardless of what the
organization calls its sign-in service.

Request `openid profile email authorization` and `response_type=code`. Generate
unique, cryptographically random `state`, `nonce`, and a PKCE verifier for each
attempt. Send the verifier's SHA-256 base64url challenge and
`code_challenge_method=S256`. Store the verifier and state server-side or in the
client library's supported secure flow state.

Verify callback state, exchange the single-use code with the original redirect
URL and verifier, and validate the returned ID token using the discovered JWKS:
only RS256, exact issuer, expected client-ID audience, expiry, and matching nonce.
Do not accept an arbitrary issuer or discovery URL supplied by a request.

The ID token includes a stable UUID `sub`, `organization_id`, `service_id`, and
the organization's `identity_name`. The `profile`, `email`, and `authorization`
scopes add profile fields, email, and roles/permissions respectively. No
`email_verified` claim is asserted. The ID token's audience is the OIDC client ID;
the access token's resource audience is the service audience returned by
introspection.

Use a central session for SSO across applications. Every service obtains its own
tokens from its own registration. A supplied `organization_id`, JWT from another
issuer, or an ID token used as a bearer access token cannot confer authority.
This implementation does not offer RFC 8693 token exchange or machine-user
impersonation.

## Authorize each resource request

Use the access token to check current permission. Introspection is an HTTPS POST
authenticated with a confidential service client's HTTP Basic credentials:

```http
POST /oauth/introspect/
Authorization: Basic <base64(form-encoded-client-id:form-encoded-client-secret)>
Content-Type: application/x-www-form-urlencoded

token=<opaque-access-token>
```

An active response has this shape (identifiers below are illustrative):

```json
{
  "active": true,
  "iss": "https://identity.customer.example",
  "sub": "user-uuid",
  "client_id": "issuing-client-id",
  "aud": "urn:core:service:service-uuid",
  "organization_id": "organization-uuid",
  "service_id": "service-uuid",
  "scope": "openid authorization",
  "token_type": "Bearer",
  "exp": 1800000000,
  "roles": ["member", "report-reader"],
  "permissions": ["read"]
}
```

Check `active` is the boolean `true`, the expected issuer, organization, service
and audience, token expiry, and the required permission. Then scope application
data queries to the verified organization. For administration, require the
reserved `admin` permission. A member's role label alone is not a capability.
Core administrators receive every registered permission for their own services;
resource tags can further restrict those permissions and administrative controls,
including for administrators.
unknown permissions are denied. Tokens without the `authorization` scope carry
no usable service permissions.

Inactive, expired, revoked, disabled, wrong-organization, and wrong-service tokens
produce `{"active": false}`. Invalid introspection client credentials produce
HTTP 401. Neither response grants access. Unreachable identity infrastructure or
malformed responses also deny access; surface a retryable service error rather
than silently granting permissions. Avoid caching authorization decisions if
immediate membership/role revocation is required.

For a direct decision, use the same Basic authentication to POST
`token`, `permission`, and the **service's configured** `organization_id` to
`/api/v1/authorize/`. It returns `{"allowed": true, "subject": "user-uuid"}`
or `{"allowed": false, "subject": null}`. The calling client's registration
fixes the service and tenant; request parameters cannot redirect its authority.

To enforce a registered application's resource tags, also submit `resource_type`,
`resource_id`, and any required `purpose_code`. Both resource identifiers are
required; unknown or inactive resources deny access. The Python reference client
accepts the same named arguments. [The security guide](security.md) explains
registration, inherited tags, MFA evidence, and explicit grants. Core's verified
`amr`, `acr`, and `auth_time` claims describe authentication assurance; live
authorization also checks provider/link changes and organization policies.

## Python reference enforcement

`identity_client.py` requires `requests` and no Django imports. It checks issuer,
organization, service, audience, expiry, and permission against live introspection:

```python
from identity_client import ResourceClient

identity = ResourceClient(
    issuer=configured_issuer,
    client_id=backend_client_id,
    client_secret=backend_client_secret,
    organization_id=configured_organization_uuid,
    service_id=configured_service_uuid,
)
claims = identity.require(bearer_access_token, "admin")
# Continue with organization-scoped administrative work using claims["organization_id"].
```

Use `Unauthenticated` for HTTP 401, `Forbidden` for HTTP 403, and
`IdentityUnavailable` for HTTP 503. Never log bearer tokens, Basic credentials,
authorization codes, invitation links, or refresh tokens.

Run the example resource service from the repository root after securely setting
`IDENTITY_ISSUER`, `SERVICE_CLIENT_ID`, `SERVICE_CLIENT_SECRET`, `ORGANIZATION_ID`,
and `SERVICE_ID` in its process environment:

```sh
uv run python -m examples.protected_service
```

The example accepts bearer tokens on `/reports` (requires `read`) and `/admin`
(requires `admin`). It is a development HTTP server, not a production server or
an OIDC browser client. Local integration requires `ALLOW_LOCAL_HTTP=1` and an
HTTP loopback issuer. Register `read` for the service and assign a role granting
it to a member to exercise the distinction between member and administrator.

## Token lifecycle

Access and ID tokens last five minutes. Refresh tokens rotate; replaying a
superseded refresh token revokes its token family. Unused refresh tokens expire
24 hours after the associated access token expires. Applications must serialize
refreshes to avoid treating concurrent refresh retries as token theft.

Use `/oauth/revoke/` to revoke a token and discard it locally. Signing out of the
Core Identity browser portal revokes that session's authentication evidence;
live authorization and refresh then deny its bound tokens. It does not clear
other applications' sessions or recall offline ID-token assertions. Applications
must clear their own sessions and revoke their tokens on application logout. Disabling
membership immediately denies further introspection, userinfo, and refresh.
Refresh cannot extend the authentication-evidence lifetime or strengthen its MFA
assurance. Configured organization session limits require a fresh sign-in.

Keep the issuer stable during product renames. Retain old public signing keys
through the lifetime of issued ID tokens plus client JWKS cache time when rotating
keys; configure `CORE_OIDC_PREVIOUS_KEY_FILES` during that overlap.
