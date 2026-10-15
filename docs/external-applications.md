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

Give a child an ordinary organization membership, then assign a role on the **Axolotl News** service with `posts-write` and, when desired, `posts-publish`. Grant the matching environment boundary tag and any additional policy tags required by that environment or service. Do not make the child an organization administrator for blog editing. An administrator receives registered service permissions, subject to Core's current tag/authentication policy.

| Capability | Required live permission |
| --- | --- |
| Read public stories | None |
| View private studio/drafts, save drafts, request draft uploads | `posts-write` |
| Publish/unpublish, copy draft media to public revision, clean up/delete media and drafts | Both `posts-write` and `posts-publish` |

Removing a role, membership or tag, disabling the installation/environment/service/client, or revoking authentication causes the next protected API request to fail. A published public story remains public until it is explicitly unpublished; it contains no Core tokens or draft-only media keys. Previously issued short-lived S3 URLs retain their AWS expiry, so finish uploads and close other editing tabs before deleting media; see the Axolotl deployment guide for cleanup behavior.

The catalog launcher opens the registered application's public homepage. The application provides its editor sign-in link. Registration does not change the application's routing or replace its data/API implementation.

## Verification

`platform_services.tests.test_external_apps` covers real local PKCE token exchange, exact CORS, confidential introspection, cross-environment rejection, ID-token rejection, live revocation, immutable client placement, permissions, registration without a runtime node, output confidentiality, and mocked SSM transaction rollback. It creates test databases only and performs no AWS or hosting calls.

`identity.tests.test_client_credentials` additionally covers bounded/expired
proofs, concurrent verification, live revocation after a warm proof, secret
rotation/rehashing and database failures. Run these locally together before
deploying changes to machine-client authentication.

### Change log — 2026-10-15

Added bounded process-local machine-secret verification reuse to remove repeated
password hashing from resource-client requests while retaining live revocation.
Successful proofs last at most 10 minutes; authorization results are never cached.
No migrations, new dependencies, shared cache or hosting changes are required.
