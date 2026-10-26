# AWS-hosted marketplace applications

An application can be installed in Core while its frontend and API remain in AWS. The reviewed package declares `external_runtime: "aws-amplify"`. Installation creates its Core authorization service and named permissions. Registration binds that installation to an existing organization environment and exact application origin, with a public browser OAuth client and a separate confidential resource client. It creates no Kubernetes workload, runtime node, workload credential, AWS resource, or subscription.

Axolotl News implements this contract through an Amplify-hosted browser application and a Lambda HTTP API. Public stories can be read anonymously. Creating drafts, uploading files, copying media and publishing use the author's Core identity and current permissions. The package is [examples/axolotl-news-package.json](../examples/axolotl-news-package.json).

## Register a reviewed package

Create the organization and administrator first using the normal Core bootstrap process. In **Central admin → Environments**, create the target environment. For Axolotl production, use:

| Field | Value |
| --- | --- |
| Name | `production` |
| Namespace | An organization-unique label such as `family-production`; this creates no namespace in a cluster |
| Application origin | `https://axolotl.news` |
| Ingress class / TLS secret name | Leave blank for this external application |
| Auto deploy | Unchecked |
| Active | Checked |

Creating the environment also creates its `environment-production` permission tag. Environment names and namespace labels become immutable. Register the final hostname from the outset: the browser callback and allowed origin are immutable for the installation/environment. A preview Amplify hostname can serve public pages before custom-domain setup; authentication is enabled at the registered hostname. A separate staging environment may use its own registration and AWS deployment.

After reviewing the local package, a local operator can export credentials into a private directory. The following command writes files and Core database records; it does not contact AWS:

```sh
mkdir -p .local/axolotl
chmod 700 .local/axolotl
.venv/bin/python manage.py register_external_application \
  --organization family \
  --environment production \
  --package examples/axolotl-news-package.json \
  --origin https://axolotl.news \
  --actor-email YOUR_EXISTING_ADMIN_EMAIL \
  --public-config .local/axolotl/core-public.json \
  --credentials-file .local/axolotl/core-resource-client.json
```

The actor must be an active administrator of that organization or an active superuser. Organization, administrator and environment must already exist. Private output files are created with mode `0600`; files inside the checkout must live under ignored `.local/`. Output files are never overwritten. The same unchanged installation/environment/origin is idempotent, so a new output filename can recover its existing client credentials. A conflicting origin, package definition, release or environment is rejected.

For the AWS runbook's dedicated one-off registration task, use its explicitly scoped SSM destination instead of an ephemeral container file:

```sh
python manage.py register_external_application \
  --organization family \
  --environment production \
  --package examples/axolotl-news-package.json \
  --origin https://axolotl.news \
  --actor-email YOUR_EXISTING_ADMIN_EMAIL \
  --credentials-parameter /axolotl-news/core-resource-client
```

This option makes an AWS SSM `PutParameter` request only when explicitly supplied. It requires the dedicated task's `CORE_REGISTRATION_SSM_PARAMETER` to equal the requested name and its role to permit that exact SecureString. `--private-output-ssm-parameter` is an alias. Existing parameters are never overwritten. Failure rolls back the database registration and removes newly created local output files. If SSM succeeds but the subsequent database commit fails, the parameter may remain: review both states before deliberately removing it and retrying. The command never automatically replaces a previously stored secret.

Stdout contains only public JSON; no public file is required. `--public-config` optionally creates a new public JSON file. Private data contains only `client_id` and `client_secret`, stored separately in the selected private file or SSM parameter. Do not place that value in browser configuration, source control, task command arguments or logs.

An administrator may also install the package and choose **Register external application** in the Applications catalog. The form asks only for an existing environment and exact origin, displays public configuration, and does not reveal the confidential client secret. The CLI exports that already registered client's credentials into the server-side destination. **Enable integration** and **Disable integration** control Core access; AWS deployment and health remain managed in AWS.

## Public and private configuration

The public registration JSON has these fields:

| Field | Meaning |
| --- | --- |
| `version` | Registration format version, currently `1` |
| `issuer` | Trusted public Core HTTPS origin |
| `organization_id`, `service_id`, `audience` | Tenant and application service boundary; audience is `urn:core:service:<service UUID>` |
| `installation_id`, `deployment_id`, `environment` | Immutable installed application placement |
| `browser_client_id` | Public PKCE client ID; it has no browser secret |
| `redirect_uri`, `application_origin` | Exact registered callback and browser origin |
| `authorization_endpoint`, `token_endpoint`, `revocation_endpoint`, `introspection_endpoint` | Core routes under the trusted issuer |
| `permissions` | Package permission vocabulary; it does not grant membership roles |

The Axolotl repository's `scripts/write-core-config.mjs` consumes this public JSON and the HTTP API HTTPS origin to write its nonsecret `nce-config.json`. The Lambda receives the same trusted issuer/organization/service plus the private SSM parameter reference. It fetches the confidential credentials server-side. Changing a browser-supplied organization, permission or service field never changes authority.

## Authentication and live authorization

1. The browser requests `/oauth/authorize/` using authorization-code flow, PKCE `S256`, a fresh `state`, its registered public client ID, exact callback, and scope `openid authorization`. Core currently requires the `openid` scope. The Axolotl application uses the opaque access token for API access and discards the ID token.
2. The browser posts code/verifier to `/oauth/token/`. Core allows CORS only for the active public client and its exact registered origin. Token/revoke preflights allow POST and `Content-Type`, with no credentialed cookies. Public-client token requests cannot carry a confidential `Authorization` header.
3. The API sends the opaque user token to `/oauth/introspect/`, authenticating the separate resource client with HTTP Basic over HTTPS. Introspection is repeated for every protected operation. Only the private client secret may be cached briefly; authorization results are not cached.
4. Core checks token expiry, current authentication evidence, active user/membership/organization/service, roles, policy tags, environment, package/installation and both registered clients. The resource client accepts tokens only from its paired browser client in the same deployment. A different environment or application pair is inactive even when it shares a service.
5. The API validates active status, issuer, organization, service/audience, subject, expiry, bearer type, `authorization` scope and current permission before touching private data. An ID token is explicitly rejected by introspection for resource authorization. A failed or unavailable Core check denies protected operations.

Core reuses successful confidential resource-client secret verification for at
most 10 minutes in each process, bounded to 256 proofs. Each proof is an HMAC
fingerprint bound to the client ID, supplied secret and freshly loaded stored
credential hash; the cache retains no plaintext credential, token or permission
result. Concurrent requests share verification of the same credential. Client,
service, organization and deployment availability are still read on every
request, followed by live token, membership and policy evaluation. Rotation or
rehashing changes the fingerprint immediately; deactivation and revocation do
not wait for this cache to expire. Human password checks are unchanged. This
requires no shared cache service or infrastructure change.

Within one introspection, Core loads shared policy facts once: current
membership/tag grants, organization authentication policy, application placement,
resource inheritance and permission tags. The evaluation scope is cleared before
the response returns and cannot be reused by a later request or copied context.
Each permission still checks its current inherited tags, authentication age,
MFA and purpose, and retains its individual audit record. No authorization result
is stored across requests. Bulk permission-tag loading avoids repeating the same
database reads for every permission granted to an administrator.

Give a child an ordinary organization membership, then assign a role on the **Axolotl News** service with `posts-write` and, when desired, `posts-publish`. Grant the matching environment boundary tag and any additional policy tags required by that environment or service. Do not make the child an organization administrator for blog editing. An administrator receives registered service permissions, subject to Core's current tag/authentication policy.

| Capability | Required live permission |
| --- | --- |
| Read public stories | None |
| View private studio/drafts, save drafts, request draft uploads | `posts-write` |
| Publish/unpublish, copy draft media to public revision, clean up/delete media and drafts | Both `posts-write` and `posts-publish` |

Removing a role, membership or tag, disabling the installation/environment/service/client, or revoking authentication causes the next protected API request to fail. A published public story remains public until it is explicitly unpublished; it contains no Core tokens or draft-only media keys. Previously issued short-lived S3 URLs retain their AWS expiry, so finish uploads and close other editing tabs before deleting media; see the Axolotl deployment guide for cleanup behavior.

The catalog launcher opens the registered application's public homepage. The application provides its editor sign-in link. Registration does not change the application's routing or replace its data/API implementation.

## Multiple organizations at one application origin

One externally hosted application runtime can serve independently registered
organization installations at the same exact public origin. Every installation
still has its own service, browser OAuth client, confidential resource client,
environment and deployment identity. Registering another organization creates
metadata and credentials; it neither provisions hosting nor copies another
organization's records, agents, roles, integrations or secrets. A private
marketplace package cannot be installed into a different organization. Use a
separate reviewed package/installation rather than making a tenant's private
package shared.

### Keep shared-runtime registrations across releases

For the reviewed Fargate recipe, Core owns the additional-registration allowlist
in the existing hosting `CloudApplication.config` field under
`core_registration_deployment_ids`. The list contains up to 19 distinct,
canonical deployment UUIDs; it contains no client secrets. Configure only exact
existing external deployments, after creating each organization's separate
private package, installation, environment and OAuth pair:

```sh
python manage.py configure_shared_application_registrations \
  --application HOST_CLOUD_APPLICATION_UUID \
  --deployment ADDITIONAL_EXTERNAL_DEPLOYMENT_UUID \
  --actor-email EXISTING_ADMIN_EMAIL
```

Repeat `--deployment` for additional organizations. The command replaces the
whole additional allowlist. Use `--clear` instead of `--deployment` to remove it.
It requires the hosting application's current human administrator authority,
including its hosting provider's platform/environment authority, and active
administrator access to every selected target organization. Configuration locks
the hosting application and refuses queued/running releases. It records the
human actor and each retained/removed application/deployment binding in Core's
append-only audit log. This command changes Core metadata only and makes no
AWS request; prepare a new reviewed release afterward to publish the bundle.

The release snapshot pins the primary and additional public registration
bindings, including organization, service, deployment, browser/resource client,
environment, exact origin and callback. Changing the allowlist or an identity
binding invalidates its approval. Every registration must remain active and
match the primary exact origin and callback; an unavailable target stops secret
export until an operator removes it or restores its authorized configuration.
This never silently restores a revoked registration.

At the normal registration/configuration step, Core rebuilds credentials from
only those current database deployments and their encrypted secret copies. It
writes the existing SSM Standard SecureString parameter with the primary
`client_id`/`client_secret` plus a `registrations` array. Each additional entry
contains `organization_id`, `service_id`, `deployment_id`, `browser_client_id`,
`resource_client_id`, `resource_client_secret`, `environment` and `name`, matching
Proposal Studio's server-side registry contract. The writer validates the exact
shape and serialized 4 KiB limit before acquiring an AWS client. The operator
command and release review also preflight current secret hashes and this size
limit before starting any metered build. The old two-key
bundle remains unchanged when no additional registration is configured.

Do not manually append organization credentials to SSM: normal releases rebuild
from Core's durable allowlist. Core never reads or merges an existing SSM value,
scans organizations, accepts a foreign secret bundle, upgrades to Advanced
Parameter Store, or adds an IAM permission. Existing runner/task secret
references remain unchanged. The size bound can require a smaller allowlist;
there is no automatic paid capacity upgrade.

Rotating an external confidential client's secret through Core also updates
only that exact deployment's encrypted server-side copy within the same database
transaction. The former secret is invalid immediately. The next reviewed release
publishes the new copy; secret exports verify it against the current stored
credential hash and reject stale/out-of-band copies. Deactivating a client or
registration continues to deny protected access immediately, regardless of an
older running application's cached configuration. No credentials enter public
configuration, release snapshots, outputs or audit records.

### Discover a user's authorized organizations

For a user-facing organization selector, the backend may call
`POST /oauth/application-contexts/` using HTTP Basic authentication with its
current confidential resource client. The JSON body is exactly:

```json
{"token": "CURRENT_USER_ACCESS_TOKEN", "deployment_ids": ["EXPLICIT_DEPLOYMENT_UUID"]}
```

The candidate list must contain one to 20 distinct deployment UUIDs from the
application operator's trusted registration allowlist. There is no implicit
organization enumeration. The source token must be a live token from the
resource client's exact paired browser/deployment and currently authorize
`workspace-use`; source deployment policy also applies. A browser client secret,
ID token, different deployment's token, or a token with only another permission
cannot authorize discovery. Optional purpose context uses `X-Core-Purpose`.

The `contexts` response includes only candidates with the source's exact public
origin and browser callback and with active external clients, deployment,
installation, environment, service, package and organization. Every candidate
requires the user's current target membership and target `workspace-use`
permission, evaluated with that target browser application's environment,
resource tags and authentication policy. Source roles or
administrator status confer no target membership or permissions. Unknown,
inaccessible and different-origin candidates are omitted indistinguishably.
Each returned item contains `organization_id`, `organization_name`, `service_id`,
`deployment_id`, `browser_client_id`, `environment` and `public_origin`. No
credentials, access/refresh tokens, or customer content are returned. Responses
are non-cacheable and do not enable browser CORS.

Discovery is metadata only and grants no target access. Switching organizations
must initiate a separate authorization-code/PKCE exchange using the selected
trusted registration, validate its exact resulting organization/service/client/
deployment claims, and isolate application sessions, storage, background jobs
and cached UI state by that binding. The selected target must use its own
confidential resource client on subsequent protected requests. A client-supplied
organization identifier never retargets a current token. Core performs no cloud
or provider request as part of context discovery.

Target OAuth consent is separate from permission eligibility: absent or revoked
target consent does not hide an otherwise eligible organization. Choosing it
starts explicit target authorization, which asks for approval again when needed.
Revoked target credentials remain invalid; discovery neither reinstates them nor
grants consent. Revocation of the source token/consent still immediately prevents
that token from authorizing discovery.

## Verification

`platform_services.tests.test_external_apps` covers real local PKCE token exchange, exact CORS, confidential introspection, cross-environment rejection, ID-token rejection, live revocation, immutable client placement, permissions, registration without a runtime node, output confidentiality, and mocked SSM transaction rollback. It creates test databases only and performs no AWS or hosting calls.

`identity.tests.test_client_credentials` additionally covers bounded/expired
proofs, concurrent verification, live revocation after a warm proof, secret
rotation/rehashing and database failures. Run these locally together before
deploying changes to machine-client authentication.

`identity.tests.test_policy_evaluation` checks constant read-query growth as
permissions increase, preserved auditing, evaluation cleanup/isolation, current
time/purpose checks, and revocation on the next HTTP request.

`identity.tests.test_application_contexts` covers same-origin independent
organizations, exact client pairing and permission names, bounded explicit
candidates, unknown/different-origin/callback exclusion, membership and consent
revocation, environment and resource tags, authentication/MFA/purpose policy,
non-cacheable secret-free output, and execution without external requests.

### Change log — 2026-10-15

Added bounded process-local machine-secret verification reuse to remove repeated
password hashing from resource-client requests while retaining live revocation.
Successful proofs last at most 10 minutes; authorization results are never cached.
Added call-scoped policy facts and bulk permission-tag loading to eliminate
repeated policy reads within a single live introspection. Revocation remains
effective on the next request; per-permission auditing is retained.
No migrations, new dependencies, shared cache or hosting changes are required.
