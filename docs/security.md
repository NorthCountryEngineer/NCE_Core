# Identity providers and permission tags

Core Platform is both an OpenID Connect provider for applications and an OIDC
broker for upstream sign-in. Local Core accounts are supported alongside
Keycloak, cloud identity providers, another Core deployment, and other compliant
OIDC providers. Organization memberships, roles, and authorization remain in
Core; upstream email addresses and groups never grant permissions automatically.

## Configure an upstream provider

An organization administrator uses **Identity providers and permission tags**
from the organization page or central admin panel. Add a provider, register its
callback in the upstream system, link existing members, then enable the provider.
The provider kind labels the integration; every kind uses the same validated
OIDC discovery and authorization-code implementation.

| Provider | Exact issuer example | Setup notes |
| --- | --- | --- |
| Keycloak | `https://sso.example.com/realms/customer` | Use the realm issuer, a confidential OIDC client, and PKCE S256. |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant-uuid>/v2.0` | Use a specific tenant, rather than `common` or `organizations`. |
| Amazon Cognito | `https://cognito-idp.<region>.amazonaws.com/<pool-id>` | Approve the hosted/custom authorization and token endpoint host as well. |
| Google | `https://accounts.google.com` | Register a web client and select `client_secret_post` if required by the provider. |
| Another Core deployment | `https://identity.parent.example` | Register Core as a confidential application in the upstream organization's service. |
| Generic OIDC | The provider's discovery issuer | Requires PKCE S256 and asymmetric RS256 or ES256 ID tokens. |

Issuer matching is exact. Issuer paths are supported; trailing slashes are
rejected. Discovery must return the configured issuer and approved HTTPS endpoints.
Microsoft and Google identity hosts and regional Cognito issuer hosts are
recognized. Operators approve additional exact hostnames with
`CORE_IDP_ALLOWED_HOSTS`, including Keycloak, other Core deployments, and Cognito
hosted domains. Wildcards are not interpreted. Only explicit development mode
allows HTTP loopback providers. Requests verify TLS, reject redirects, and have
timeouts and bounded response sizes. Provide trusted runtime CA roots when using
private identity infrastructure; do not disable certificate verification.

Provider client secrets are mounted files, never values entered into the portal:

```text
CORE_IDP_SECRET_ROOT=/run/secrets/identity-providers
/run/secrets/identity-providers/<organization-uuid>/customer.secret
```

Each provider's **client secret file** must be an absolute path beneath its own
organization's folder. Resolved paths cannot escape that folder, including through
symlinks. Mount files read-only, readable only by the service account. Organization
UUIDs are shown on the security page. Development defaults to `.local/idp-secrets/`.
Providers default to disabled. Changing configuration increments its version and
invalidates existing authentication evidence; replace a provider to change an
issuer or client ID after subjects have been linked. Secret rotation at the same
file path does not itself advance that version; disable and re-enable the provider
when rotation should also end existing sessions.

The registered callback is:

```text
https://<configured-core-issuer-host>/accounts/providers/<provider-uuid>/callback/
```

Use the exact callback shown by that route, not a wildcard. Configure code flow,
PKCE S256, and the selected `client_secret_basic` or `client_secret_post` token
authentication method. Core checks state, nonce, issuer, audience, authorized
party, expiry, claim types, and the provider's signing key. State is bound to the
browser session, expires after ten minutes, and is consumed on the first callback
attempt. Upstream access/refresh tokens are not retained.

Link an upstream **subject** (`sub`) to an existing active Core member. Subject
identifiers are exact and provider-specific. A matching or verified email does
not create or link an account. Invitations and local account provisioning remain
the enrollment paths. A disabled link, provider, user, or membership denies
subsequent live authorization checks. Changing a link revokes its old evidence.

## Authentication assurance

Provider MFA mappings are explicit lists of trusted `amr` or `acr` values. Core
accepts MFA only from the verified provider token when a configured value matches.
Do not map an ordinary password or an unverified assurance level to MFA. Keycloak
authentication flows and claim mappers must emit an assurance value that actually
means MFA. Some cloud providers do not emit an appropriate claim; use a broker or
provider configuration that does before enforcing MFA. Native password sign-in
does not assert MFA. Headers and workload credentials cannot assert human MFA.

Organization authentication policy can require federation, verified MFA, and an
absolute authentication-evidence lifetime (default eight hours). The portal
requires the administrator to establish the requested assurance before enabling
it organization-wide. Tag policies can additionally require a recent upstream
`auth_time`; receiving a new token does not make an old authentication fresh.
**Authenticate again** requests `max_age=0` and checks a recent `auth_time`.

Core-issued ID tokens publish `amr`, `acr`, and, when verified, `auth_time`.
Verified MFA is emitted as `amr: ["mfa"]` and
`acr: "urn:core:assurance:mfa"`, allowing another Core deployment to broker it
when that deployment explicitly trusts these mappings. Refresh preserves the
original evidence; it cannot upgrade password sign-in to MFA. Authorization-code
redemption and refresh also recheck live application tags and their grants, using
the purpose declared during authorization. A removed grant cannot be restored
by refreshing an existing token.

Core portal logout revokes its authentication evidence. Live introspection,
userinfo, refresh, and queued human workflow checks then deny credentials bound
to that evidence. Applications must still clear their own sessions and perform
live authorization: an already-issued signed ID token cannot be recalled from
an application that only validates it offline. Tokens issued before evidence
binding was introduced require a new sign-in to refresh.

Upstream account disablement is not polled on each resource request. Disable the
Core membership/link/provider for immediate Core revocation and use suitable
session lifetimes. Direct upstream back-channel logout and SCIM provisioning are
not implemented. SAML/LDAP providers can connect through an OIDC broker such as
Keycloak; direct integrations currently use OIDC, not every identity protocol.

## Resource requirements and explicit grants

**Permission tags** attached to a resource are requirements. **Access-tag grants**
on a membership or its organization roles satisfy those requirements. A caller
must still hold the service permission or workload capability needed for the
operation. Every applicable tag must pass; matching one tag is insufficient.
Organization administrators and platform superusers do not bypass tag controls.

A tag has a classification (`public`, `internal`, `confidential`, `restricted`),
optional MFA requirement, optional authentication age limit, allowed purpose
codes, and an explicit workload allowance. Classification is a label; enforcement
comes from grants and the configured requirements. Even a public tag requires
an explicit grant when attached to a resource.

For example, a `phi` tag could be restricted, require MFA and authentication within
300 seconds, and allow purposes `treatment` and `operations`. Grant the tag to the
appropriate care-team role and attach it to the protected dataset. Administrators
without that grant and assurance are also denied. Workloads cannot satisfy human
MFA or freshness requirements, even if **allow workloads** is selected. Machine
access needs a deliberately configured tag without those human requirements,
explicit **allow workloads**, installation/runtime grants, and normal capabilities.

Set up grants and verify access before attaching restrictive tags to organization
roots or critical controls. Existing resources must pass their current policy
before their tags can be removed or edited. Editing an existing tag's requirements
also requires that tag's grant and assurance. A retired tag denies data access;
authorized administrators can still edit its controls to reactivate it.

Tags cover organization metadata, membership and role controls, services and
service permissions, OAuth applications, invitations, providers, billing and
deployment configuration, installations, runtimes, deployments, workload keys,
datasets, pipelines, agents, workflows, and runs. Use the adjacent **Tags and
grants** links or the security page's resource picker. User authorization is
organization-specific through membership; global account administration and the
global marketplace catalog remain operator controls.

Resources inherit ancestor tags: organization, service, installation, runtime,
dataset, and run boundaries as applicable. Data records inherit dataset tags;
step outputs inherit run tags. A queued run captures source requirements and
rechecks live grants, authentication evidence, and current source requirements
before executing each step. Run output also inherits current source tags, so
adding a tag to a source dataset protects results produced before classification.

Purpose codes are exact configured identifiers, never patient names or free text.
The portal's **Access purpose** selector stores a declaration for that organization
in the browser session. API clients supply `X-Core-Purpose`; OAuth introspection
and authorization use the `purpose_code` form field. A purpose declaration does
not grant permission or prove a clinical need; applications still constrain their
workflows and operators review the audit trail.

## Protect application-owned resources

A service administrator can register a **protected application resource** using
an opaque external identifier and resource type, then assign tags. Core stores
policy metadata, not the application's record content. The service's central
authorization request includes both `resource_type` and `resource_id`; unknown,
inactive, foreign-service, or unsatisfied resources deny access.

```python
claims = identity.require(
    bearer_access_token, "read",
    purpose_code="treatment",
    resource_type="patient-record",
    resource_id=opaque_record_id,
)
# Query only the verified organization and authorized record.
```

The reference `ResourceClient` performs live introspection and a central resource
decision, pins the tenant/service/subject, and denies malformed responses or
outages. Calls without resource context enforce service permissions only. An
application must supply the resource context for every protected record operation
and enforce its data-query boundaries; listing metadata or possessing a JWT is
not a substitute for authorizing the operation.

## Audit and compliance preparation

Protected decisions and denials record organization, resource UUID, stable actor
or workload/runtime UUID, request UUID, outcome, reason, tag codes, purpose code,
and time. `X-Core-Request-ID` correlates HTTP requests. Administration, sign-in,
logout, issuance, and workflow-denial events are recorded too. Audit entries avoid
tokens, secrets, resource payloads, upstream claim documents, and patient content.
Capture the stable actor UUID even if an account is subsequently deleted.

Audit models reject application-level updates/deletions; Django admin is read-only
and organization deletion cannot cascade away audit records. This is not database
immutability: privileged database operators and raw SQL can bypass ORM protections.
An operator can export ordered JSONL batches for controlled archival:

```sh
uv run python manage.py export_security_audit \
  --organization customer --after-id 0 --limit 10000 > /secure/archive/core-audit.jsonl
```

Choose archival storage, access controls, encryption, retention, and monitoring
under the organization's operational policy. Nothing is transmitted or purchased
automatically. A deployment pursuing HIPAA needs its own assessment, applicable
provider agreements, backup and recovery controls, audit retention and review,
and incident procedures. These controls prepare the authorization foundation;
the software and configuration alone do not establish HIPAA compliance. Native
MFA enrollment, account recovery delivery, emergency-access approval workflows,
and field-level data encryption are not implemented.
