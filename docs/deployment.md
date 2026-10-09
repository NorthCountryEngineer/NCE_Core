# Deploying Core Platform

The development server is prepared locally. A customer deployment requires a
domain, TLS termination, PostgreSQL, securely mounted keys, and application client
registrations. Deploying or migrating a live customer system is a separate step.

For configured releases through GitHub Actions, GitLab, Azure Pipelines, Jenkins,
CircleCI, Bitbucket, or AWS CodeBuild/CodePipeline, see [the CI/CD guide](cicd.md).
It supplies shared stages, digest-pinned Kubernetes resources, a migration gate,
web/worker rollout checks, and HTTPS smoke checks against existing infrastructure.

## Configuration

| Variable | Use |
| --- | --- |
| `CORE_DEBUG` | `1` enables local development. Unset or `0` is production. |
| `CORE_ISSUER` | Fixed HTTPS origin, such as `https://identity.customer.example`; no path, query, or trailing component. |
| `CORE_ALLOWED_HOSTS` | Comma-separated hostnames; defaults to the issuer hostname. |
| `CORE_PRODUCT_NAME` | Deployment-wide display name; defaults to Core Platform. |
| `CORE_SECRET_KEY_FILE` | Mounted file containing a unique Django secret key. Required in production. |
| `CORE_OIDC_KEY_FILE` | Mounted file containing the deployment's RSA private signing key in PEM format. Required in production. |
| `CORE_OIDC_PREVIOUS_KEY_FILES` | Comma-separated old signing-key file paths for key-rotation overlap. |
| `DATABASE_URL` | PostgreSQL connection URL. Required in production; supports query options such as `sslmode=verify-full`. |
| `CORE_TRUST_PROXY` | Set `1` only if a trusted proxy strips and sets `X-Forwarded-Proto`. |
| `CORE_IDP_ALLOWED_HOSTS` | Additional exact upstream OIDC endpoint hostnames, comma-separated. |
| `CORE_IDP_SECRET_ROOT` | Mounted provider secret root, with one folder per organization UUID; production default `/run/secrets/identity-providers`. |

Generate unique production secrets through your secret management system. Use an
RSA key of at least 3072 bits. Mount secret files read-only and make them readable
only by the service account. Do not reuse development keys, check secrets into Git,
or put them in Docker build arguments. Keep database passwords in runtime secrets.
Use verified TLS for remote PostgreSQL connections and provide the database CA.

Core Identity refuses production startup without the issuer, key files, and
PostgreSQL configuration. It enables secure cookies, HTTPS redirects, HSTS, CSRF
protection, and explicit host validation. Ensure the issuer's subdomains support
HTTPS before applying the included HSTS subdomain/preload policy.

## Install and start

With Python 3.12 and uv, from the repository root:

```sh
uv sync --frozen --no-dev
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput
uv run python manage.py check --deploy --fail-level WARNING
uv run gunicorn core.wsgi:application --bind 0.0.0.0:8000 --workers 2
```

Run `uv run python manage.py core_worker` as a separate supervised process using
the same database and keys. The worker executes persisted agent workflows and
heartbeats embedded core instances. Without it, jobs remain queued and embedded
instances become stale. PostgreSQL supports multiple worker processes; begin with
one and scale only when needed. SQLite is for local development.

Supply runtime configuration before these commands. Run migrations once as a
release operation, not independently on every worker's startup. Scale worker
count to available memory and database connection limits. `/health/` checks the
database; verify discovery, login, and a real OIDC client flow before directing
users to a new deployment.

The Dockerfile builds a non-root image with frozen dependencies:

```sh
docker build -t core-platform .
```

Builds behind a verified corporate TLS proxy may supply its CA with
`--secret id=build_ca,src=/path/to/proxy-ca.crt` and use Docker's standard proxy
build arguments. The optional CA is used only during dependency installation,
combined with system roots, and is not retained in the image. Runtime proxies
need separately configured, verified trust as appropriate for the deployment.

Pass the above runtime variables and mount secret files when running the image.
The default container service account has UID/GID 10001; grant that account
read access to mounted keys while keeping those keys inaccessible to other users.
Use the image's `.venv/bin/python manage.py` for release commands. Persist the
PostgreSQL database and backups outside the application container. Serve the
collected `staticfiles/` files through your reverse proxy or static asset host;
the production Gunicorn process does not serve Django admin assets.

Use the same image for a worker container with command
`.venv/bin/python manage.py core_worker`. Connect existing Kubernetes clusters
using [the namespace-scoped runtime connector](platform.md#connect-kubernetes).
For a Kubernetes controller, label its Deployment with
`core.platform/organization=<organization-uuid>` before adopting it in the central
panel. Its rolling restart preserves its image, environment, database, and mounted
keys. Controller image releases and database migrations remain operator release
steps, so a controller cannot replace its own storage or stop itself accidentally.

Automatically provisioned integration bundles are encrypted in the database using
a key derived from `CORE_SECRET_KEY_FILE`. Back up that key with the database.
Rotating it requires decrypting and re-encrypting every bundle as a controlled
offline migration before switching the key; changing it directly makes bundles
unreadable. OIDC signing-key rotation is independent of this encryption key.

The HTTP listener must be reachable only through the trusted TLS proxy. Restrict
database access to the identity service and database operators. Rate-limit login,
token exchange, invitation acceptance, and client-authenticated API endpoints at
the ingress. Filter codes, credentials, and invitation tokens from access logs;
invitation tokens are carried in URL paths. Secure monitoring and audit storage,
database backups, and signing-key backups. The portal records organization
administration activity without storing invitation tokens or client secrets in
audit events.

## Provisioning and trust

Use `bootstrap_organization` to provision an organization's first administrator,
then set its password interactively. Subsequent organization access is managed
by that administrator. Use a separate, tightly controlled platform superuser for
deployment administration. Organization administrators cannot create organizations,
modify global account privileges, or access another organization's controls.
Operator-approved developers may create their own organization through onboarding;
this does not grant platform privileges or access to existing organizations.

Each customer may deploy its own instance with its own stable issuer and keys.
Services explicitly trust that configured issuer. Core supports local accounts
and upstream OIDC sign-in, including Keycloak, cloud identity providers, and
other Core deployments. Set up mounted client secrets, exact subject links, and
verified MFA mappings as described in [the security guide](security.md). Live
authorization remains in Core. Email delivery/account recovery, native MFA
enrollment, OAuth client-credentials grants, and token exchange require additional
implementation. Scoped workload identities for core APIs are supported separately.

Changing the display name is supported without breaking trust. Changing issuer
origins is a trust migration: coordinate client configuration and sessions instead
of simply renaming the display name or forwarding old tokens.
