# CI/CD for Core Platform

The repository defines one portable pipeline in `ci/pipeline.py`. Provider adapters
call `bash ci/run.sh`; tests, image publication, and Kubernetes release behavior
remain the same whichever CI platform runs it. A pipeline run builds one image,
publishes it, and deploys that exact registry digest without rebuilding it between
those stages. Separate environments have separate reviewed configuration files.

GitHub runs `.github/workflows/verify.yml` on every pull request and push. Its
**Core required checks** job validates the AWS templates and runs the existing
verification pipeline with a disposable PostgreSQL database. It has read-only
repository access, a bounded timeout, no deployment secrets, and no publish or
deploy step. Concurrent checks for the same ref cancel superseded checks.

The separate `.github/workflows/pipeline.yml` release workflow remains manual-only
and defaults to an existing self-hosted runner. Automatic verification uses a
standard GitHub-hosted Linux runner; other provider templates are inert until
installed. These workflows create no AWS services, persistent runner capacity,
registries or clusters. Obtain the owner's explicit
cost approval before activating any billable runner, artifact storage, registry,
cloud build, or deployment capacity. AWS CodeBuild/CodePipeline are normally
billable even for manual runs. An external platform's webhook settings can trigger
builds independently of this repository; disable those until approved.

## Exact-commit deployment admission

`platform_services/github_ci.py` is the shared, fail-closed GitHub Actions gate.
Core source defines two policies: `NorthCountryEngineer/NCE_Core` requires
`.github/workflows/verify.yml` / **Core required checks**;
`NorthCountryEngineer/Proposal_Studio` requires `.github/workflows/ci.yml` /
**Proposal Studio required checks**. Application manifests and browser input
cannot disable or replace these policies.

A deployment requires a successful **push** run for each exact 40-character source
SHA: both the application and its approved Core runner. Pull-request merge
checks remain useful review feedback, but cannot substitute for the pushed
release commit. The gate checks GitHub's workflow identity/path, repository,
`head_sha`, event, latest updated run and attempt, and the required aggregate job.
It checks again after reading the attempt's jobs. A pending, failed, cancelled,
skipped, missing, ambiguous, or inaccessible result blocks deployment; an old
success does not override a newer failed rerun.

If GitHub's workflow-specific index returns a well-formed empty result for the
exact push commit, Core reads the repository-wide index with the same SHA and
event filters. This alternate index must be complete within 100 runs. Core
checks every returned run's repository, source repository, commit, event, IDs and
timestamp before selecting the exact trusted workflow ID and path. Duplicate IDs
and crossed workflow identities are refused. Other workflows cannot supply a
success or hide a newer pending or failed attempt of the required workflow.
The workflow itself must still be completed successfully, followed by the exact
attempt's required successful aggregate job. The final consistency check stays
on the repository index once it was needed; it never switches to an index that
may omit newer runs. Empty results from both indexes, malformed/truncated data,
access errors and any non-successful trusted result continue to block release.
This performs bounded read-only GitHub requests and creates no CI or cloud jobs.

The cloud console checks during release preparation, approval, and immediately
before acquiring AWS credentials or starting each CodeBuild phase. A refusal
before StartBuild never creates or recovers a billable AWS build. The pinned
runner separately verifies its own Git checkout and the application checkout.
Core's portable `publish` and `deploy` commands use the same gate before registry
or cluster mutation. The AWS Core update helper requires `--commit` and checks
that source SHA before writing parameter files plus a private `core-ci.json`
receipt. Image digests and their source labels must still be reviewed together.

Public GitHub CI metadata needs no token. For private Proposal Studio source,
a human operator creates a fine-grained credential restricted to the approved
repository with **Actions: read** and **Metadata: read**, and enters it in
**Deployment platforms → Edit → GitHub CI read credential**. Core encrypts it
with its existing database secret protection; the form never reads it back.
Blank input preserves it, replacement rotates it, and the explicit remove box
revokes it. Changes invalidate platform verification and existing release plans.
This adds no AWS services, IAM permissions, secret mounts, or subscriptions.

CLI operators can instead set `CORE_CI_GITHUB_TOKEN_FILE` to a protected temporary
credential file; `GH_TOKEN` and `GITHUB_TOKEN` are never implicitly consumed.
The live console uses only the current server-side platform credential and never
falls back to that CLI file. Tokens are sent only to fixed `https://api.github.com`
requests, with redirects and environment proxies disabled, bounded response sizes
and timeouts. They never enter snapshots, AWS build variables, artifacts or logs.
Test subprocesses discard token-file and ambient GitHub-token variables.

Run the gate manually without starting a build or deployment:

```sh
python3 ci/github_gate.py --repository NorthCountryEngineer/NCE_Core --commit FULL_SOURCE_SHA
```

This command only reads GitHub. GitHub-hosted private-repository Actions minutes
still depend on the account's included allowance and billing cap; the workflow
does not configure billing or purchase capacity. Keep paid overage disabled when
zero additional spend is required.

## Pipeline contract

| Stage | Required result |
| --- | --- |
| Configure | Validate the selected JSON configuration against `ci/config.schema.json`. |
| Check | Scan tracked files for secrets, initialize development-only test keys, run Django checks, and reject migration drift. |
| Test | Run the application suite against isolated PostgreSQL and the CI/CD guard tests. |
| Build | Use frozen dependencies and a digest-pinned Python base; label and tag with the full Git commit. |
| Publish | Require explicit enablement and the current release branch tip; authenticate with stdin or an existing credential helper; capture the registry digest. |
| Validate deployment | Check the configured context, Kubernetes TLS verification, existing secret references, and server-side manifest validation. |
| Migrate | Acquire the namespace release lease; run production checks and one bounded migration Job before changing workloads. |
| Roll out | Update web and worker Deployments, ClusterIP Service, and TLS Ingress; wait for both rollouts. |
| Smoke | Verify HTTPS health, exact OIDC issuer, signing keys, sign-in page, and unauthenticated API rejection. |

Modes are `verify` (check/test/build), `publish` (verify plus publication), and
`deploy` (publish plus Kubernetes release and smoke checks). Release modes require
a clean checkout matching the configured remote branch tip. Provider credentials
must only be exposed to trusted code and protected release branches/environments.
Do not approve a secret-bearing pipeline on an untrusted branch or fork. Use
platform environment approvals for production; separate staging/production
credentials and namespace permissions.

`CORE_CI_ALLOW_PUBLISH=1` and `CORE_CI_ALLOW_DEPLOY=1` are independent explicit
switches; both are required for `deploy` mode. They are disabled by default and
are not substitutes for financial approval or platform access policy. The shared
pipeline rejects unauthorized release modes before registry/deployment mutations.
Runner allocation happens in the CI provider, before repository code executes.

## Runner requirements

Use Linux with Python 3.12, `python3-venv`/pip, Git, Docker with BuildKit/buildx and
an existing daemon. Deployment additionally needs `kubectl` compatible with the
target cluster. Maintain these tools on an approved runner image or self-hosted
host; the pipeline does not silently install cluster clients or create a daemon.
`ci/run.sh` creates an isolated `.ci-tools` environment, installs pinned uv, and
runs `uv sync --frozen`. Test keys and generated artifacts stay out of Git and
container build contexts. Never run the pipeline in a production application's
working directory.

Verification runs pinned `detect-secrets` against every tracked file before
building. Its baseline contains reviewed false positives: secret resource names,
credential binding names, and route names. It contains hashes, never credential
values. A new finding fails verification; do not baseline a real credential.
To check before committing, run `.venv/bin/python ci/scan_secrets.py --staged` after
staging changes. The hook scans index contents, including changes that differ
from the working copy. Enable the
repository's local guard with `git config core.hooksPath .githooks` after setup.
Network
credential verification is disabled, and scanner diagnostics are captured to
avoid exposing newly detected values in CI logs.

The application suite includes Core Chat and a real HTTP federation test with an
independent disposable authority. Optional `chat_federation_secrets` mount peer
credentials under `/run/secrets/chat-federation/<organization-uuid>/` using the
same scoped file structure as `provider_secrets`. Configure approved origins with
`extra_environment.CORE_CHAT_FEDERATION_ORIGINS`; see [Core Chat](chat.md).

Supply a **dedicated, disposable PostgreSQL** connection as `DATABASE_URL` with
permission to create/drop its Django test database. Isolate it per runner/job;
never point this at a customer database or share its test database across parallel
jobs. GitHub runs `python3 ci/verify_with_postgres.py` to start a loopback-only
Docker PostgreSQL container with a cryptographically random password per run.
It passes the password through Docker stdin, keeps the connection URL in the
verification subprocess environment, and removes the container and its volume
afterward. Other adapters use the runner's configured isolated PostgreSQL service
through protected variables, or can use this same wrapper for their verification
stage and keep publication/deployment as separate steps.

## Environment configuration

Copy `ci/environments/example.json` to a tracked environment file, replace the
example domain/context/registry/secret names, and review it before release. Select
it with `CORE_CI_CONFIG`. Use one release name per namespace/application, with the
same name across providers when they target the same deployment.

The schema covers environment name, fully qualified image repository, trusted
release branch, exact kubeconfig context, existing namespace, stable HTTPS issuer,
Ingress class/TLS secret, mounted runtime and database secret names, optional image
pull secrets, per-organization provider secret mounts, replica counts, and bounded
migration/rollout timeouts. Optional `organization_id` labels the controller for
adoption in Core's central admin panel. Optional `extra_environment` supports
product branding and approved identity-provider hosts; it cannot override
production security settings or contain credentials.

Each cluster must already have its namespace, working ingress controller, DNS,
TLS, database, and secrets. The generated Service is `ClusterIP`; no load balancer,
cluster, volume, database, or payment provider is purchased or provisioned.

| Existing secret | Required contents |
| --- | --- |
| `runtime_secret` | `django.key`: stable Django/encryption secret; `oidc.key`: RSA signing private key. |
| `database_secret` | `DATABASE_URL`: production PostgreSQL URL; use verified TLS for remote databases. |
| `tls_secret` | A Kubernetes TLS secret for the configured issuer hostname. |
| `pull_secrets` | Registry pull credentials when the image repository is private. |
| Optional provider secrets | Referenced key containing the upstream OIDC client secret. |

Do not put any secret values in environment JSON, Git, Docker build arguments,
artifacts, or command-line passwords. Provision secrets using the organization's
secret manager/operator process. Keep the Django encryption key stable across
releases and back it up with the database. Coordinate issuer/signing-key changes
as trust migrations, not cosmetic configuration edits.

For provider mounts, add entries such as:

```json
{
  "secret_name": "customer-oidc",
  "organization_id": "11223344-5566-4788-9900-aabbccddeeff",
  "key": "client.secret",
  "filename": "client.secret"
}
```

The file mounts at
`/run/secrets/identity-providers/<organization-uuid>/client.secret`. File `subPath`
mounts preserve the tenant path validation described in [the security guide](security.md).
These mounts do not update in running pods when a Kubernetes Secret changes:
coordinate provider/session invalidation and restart web/worker pods after rotation.

The listener trusts `X-Forwarded-Proto` because it sits behind the configured
ingress. Restrict direct pod/service access and configure ingress to strip and
set that header. Production Django admin static assets must be served separately
by the existing reverse proxy or approved static host, as described in
[deployment instructions](deployment.md); Gunicorn does not serve them.

## Shared variables and outputs

| Variable | Purpose |
| --- | --- |
| `CORE_CI_MODE` | `verify`, `publish`, or `deploy`; defaults to `verify`. |
| `CORE_CI_GITHUB_TOKEN_FILE` | Operator-only protected GitHub Actions read credential file for release CLI checks; never passed to tests or AWS builds. |
| `CORE_CI_CONFIG` | Tracked environment JSON; defaults to the example. |
| `CORE_CI_ENVIRONMENT` | Optional protected CI environment name; when set, must match the JSON environment before publication/deployment. |
| `DATABASE_URL` | Disposable CI PostgreSQL; never the production database. |
| `CORE_CI_ALLOW_PUBLISH` | Explicit `1` permits registry publication. |
| `CORE_CI_ALLOW_DEPLOY` | Explicit `1` permits Kubernetes mutation. |
| `REGISTRY_USERNAME`, `REGISTRY_PASSWORD` | Protected registry credentials; omit both when using an existing runner credential helper. |
| `KUBECONFIG` | Secure kubeconfig file path, not its contents; context comes from environment JSON. |
| `CORE_KUBECTL` | Optional path to a maintained kubectl binary. |
| `CORE_CI_CA_BUNDLE` | Optional trusted CA bundle file for HTTPS smoke checks; TLS verification stays enabled. |
| `CORE_BUILD_CA_FILE` | Optional corporate CA file passed only as a BuildKit secret. |
| `CORE_BUILD_NETWORK`, `CORE_BUILD_PROXY_HOST` | Optional runner-specific Docker build connectivity overrides. |

`artifacts/build.json` records commit, image tag, configuration hash, and whether
the checkout was dirty. `release.json` binds that commit/configuration to a
registry digest. `deployment.json` contains the actual reviewed resources and
secret **references**, never secret values. `deployed.json` is written only after
successful migrations, rollouts, and HTTPS smoke checks. Artifacts are ignored
locally. Enable provider artifact retention/upload only after its storage costs
are approved; limit retention and access appropriately. GitHub upload is off
unless `CORE_CI_ALLOW_ARTIFACT_UPLOAD=1` is configured as a repository variable.

## Provider adapters

| Platform | File and configuration |
| --- | --- |
| GitHub Actions | `.github/workflows/verify.yml`: automatic PR/push verification. `.github/workflows/pipeline.yml`: manually select release mode and config. Register an existing runner with `self-hosted`, `linux`, `x64`, `core-ci` labels. Optional `CORE_RUNNER_LABELS` repository variable is a JSON label array. Put secrets and enablement variables in a protected environment; set `CORE_CI_ENVIRONMENT` and `CORE_KUBECONFIG_PATH` as needed. |
| GitLab CI | Copy `ci/providers/gitlab-ci.yml` to `.gitlab-ci.yml`. Register a Linux shell runner tagged `core-ci`; define protected variables and use Run pipeline. Push/MR events are excluded. Use protected environments/manual approvals for production. |
| Azure Pipelines | Register `ci/providers/azure-pipelines.yml`; configure existing pool `core-ci`, protected variable group `core-ci-settings`, environment approvals, and `CORE_KUBECONFIG_PATH`. Define all referenced variables, using empty registry values when relying on a credential helper. No push/PR triggers. |
| Jenkins | Select `ci/providers/Jenkinsfile` as the pipeline script; configure agent label `core-ci` and credential IDs `core-ci-test-database`, `core-ci-registry`, `core-ci-kubeconfig`. Build with Parameters; publication/deployment approval switches default off. Disable external webhook/polling triggers. |
| CircleCI | Copy `ci/providers/circleci.yml` to `.circleci/config.yml`; replace the runner resource class and define protected context `core-ci-protected`. The approval job precedes executor allocation. Supply pipeline mode/config parameters and protected enablement variables. Disable external automatic triggers until approved. |
| Bitbucket Pipelines | Copy `ci/providers/bitbucket-pipelines.yml` to the repository root. Configure an existing Linux shell runner labeled `core.ci`, staging deployment variables, registry credentials, and kubeconfig path. Only custom manual pipelines are declared. |
| AWS CodeBuild / CodePipeline | Use `ci/providers/buildspec.yml` as the buildspec of an approved existing Linux CodeBuild project with Git source metadata, Python 3.12, privileged Docker/buildx, kubectl, and isolated PostgreSQL. Set protected variables/secret-manager bindings. It can be a CodePipeline build action with separate approval stages. No AWS resources are created by this repository. |

Hosted CI platforms still need configuration and credentials in their own control
panels. The repository does not install Jenkins plugins, register runners, create
cloud projects, or configure provider billing. GitHub requires read access for
checkout, registry push credentials for publication, and a namespace-specific
Kubernetes credential for deployment. Add short-lived provider credential helpers
or workload federation on runners when available; no long-lived cloud API key is
required by the shared pipeline itself.

## Kubernetes permissions and release coordination

Render a namespace-scoped CI service account/role for operator review:

```sh
.venv/bin/python ci/pipeline.py rbac --config ci/environments/staging.json > /tmp/core-ci-rbac.json
# An existing cluster operator reviews/applies this file and issues a credential.
```

The role manages Deployments, Jobs, ConfigMaps, Services, Ingresses, and release
Leases. It can get only the named existing secrets and cannot create namespaces,
ClusterRoles, or secrets. Kubernetes workload creation can nonetheless reference
other secrets in the namespace: use dedicated environment namespaces and protect
this powerful operator credential. Application/runtime connector credentials
are separate and cannot authorize CI releases.

A compare-and-swap Kubernetes Lease coordinates releases across providers.
Healthy runners renew it; lost ownership stops advancement. A failed runner's
lease expires after two minutes. Inspect any still-running migration Job before
retrying after a runner failure; a lease is not a database transaction or a way to
cancel a migration during a network partition. Jobs have no automatic retries and
are cleaned up by Kubernetes after one day. An identical failed Job requires
operator diagnosis/retry or a corrected source commit.

Use backward-compatible expand/contract database migrations because old web and
worker processes can run while migrations execute. A migration failure keeps
existing Deployments untouched. A rollout/smoke failure fails the pipeline and
does not write a successful deployment artifact; some resources may already have
changed. There is no automatic database rollback. Restore application code with
a reviewed revert/new release after checking schema compatibility and backups;
inspect cluster rollout state before any manual rollback.

## Local validation and manual stages

With an existing disposable PostgreSQL configured:

```sh
CORE_CI_MODE=verify bash ci/run.sh
.venv/bin/python -m unittest discover -s ci/tests -v
```

Individual stages support splitting trusted build/release jobs:

```sh
.venv/bin/python ci/pipeline.py check --config ci/environments/staging.json
.venv/bin/python ci/pipeline.py test --config ci/environments/staging.json
.venv/bin/python ci/pipeline.py build --config ci/environments/staging.json
.venv/bin/python ci/pipeline.py render --config ci/environments/staging.json \
  --image registry.example/core@sha256:<64-hex-digest> > /tmp/core-deployment.json
```

Transfer artifacts securely between jobs and use the identical clean commit and
configuration for publication/deployment. Their bindings deliberately reject
stale images and a different destination configuration. Do not use placeholder
digest strings or the example environment for a real release. Local guard tests
check disabled mutations, credential handling, tenant secret mounts, stale
artifacts, lease contention, and migration-before-rollout ordering. Provider
templates require their platform's own configured runner for a live provider run;
local verification does not establish that seven remote CI accounts are active.

Create independent configurations, namespace-scoped runner credentials, runtime
and database secrets, and approvals for sandbox, development, and production.
Generated manifests include `core.platform/environment` and `CORE_ENVIRONMENT`.
The deployment pipeline does not create namespaces or isolate their networks;
review/apply the [environment isolation bundle](modernization.md) separately.
