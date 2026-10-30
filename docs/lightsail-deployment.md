# Lightsail consolidation and operating guide

Prepared October 27, 2026. This document describes the approved consolidation
target and locally tested deployment handling. Live cutover, authenticated
acceptance and old-resource retirement require their own dated evidence; a
successful local test does not establish those outcomes.

Core keeps its canonical issuer `https://core.nnytech.ai`, existing PostgreSQL
data, Django encryption key, OIDC RSA signing key, origin-boundary secret, user
accounts, organization memberships, roles, OAuth clients, consent records,
integration ciphertext and existing application registration bindings. Core's
web and worker run as separate supervised containers on the same server as
Proposal Studio and NCAcademy. Each application retains its own database and
database identities. Academy's normal API keeps its restricted runtime identity;
its owner credential is available only to explicit migration operations.

The current migration workspace is
`/Users/ericyager/Documents/ChatGPT/Axolotl News/ops/lightsail-migration`.
Its Core source was extracted from the deployed immutable image
`sha256:0482163f6060fd2ccae5ac9e4ad9ece5b291cda090f6accf64bebbcca96285cc`,
revision label `d3adefe79f0fbfa8b34441cc33ec9294e01807d6`. Source manifests
record the baseline and intentional portability patches. Do not deploy the
unrelated in-progress changes in the normal Core checkout.

## Server layout and process configuration

The fixed deployment home is `/srv/nce`, Compose project `nce`, base document
`/srv/nce/compose.yml`. Services are `postgres`, `core-web`, `core-worker`,
`proposal-api`, `proposal-worker`, `sam-browser` and `academy-api`. PostgreSQL 18
hosts separate `ncecore`, `studio` and `academy` databases. Private runtime files
hold secrets; keys and database/object-store credentials never belong in source,
images, shell arguments or public receipts.

The portable Core launcher accepts the original secret environment names or
mounted `CORE_SECRET_KEY_FILE`, `CORE_OIDC_KEY_FILE` and
`CORE_ORIGIN_SECRET_FILE`, plus a direct `DATABASE_URL`. Keep
`CORE_TRUST_PROXY=1`, `CORE_AWS_ORIGIN_BOUNDARY=1` and the canonical issuer. The
reverse proxy overwrites forwarded/origin headers and injects the private origin
secret. Only ports 80/443 are public. Core middleware serves the collected static
files. Migrations run once as an explicit release operation; normal web/worker
startup never runs them.

## Existing application release workflow

Existing application UUIDs, immutable placement/archetype fields, public origins,
installation/service/deployment IDs, public/confidential clients, encrypted
credentials, multi-organization registration allowlists, permissions and release
history are retained. Operator-owned bindings select Lightsail hosting for the
two migrated container applications. Other AWS static/serverless recipes retain
their existing adapter and resources.

`CORE_LIGHTSAIL_BINDINGS_FILE` identifies a read-only public configuration with
version `1`, fixed SSH runner settings and an `applications` mapping by exact
application UUID. Each binding contains exact `platform_id`, `organization_id`,
`environment_id`, `repository`, existing `archetype`, canonical `origin`,
`api_url`, existing `identity` contract and `runtime_digest`. The runtime digest
pins the fixed service/frontend destination. Core pins its binding digest into the
reviewed release snapshot. A changed binding invalidates approval. Enable
`CORE_BLOCK_UNBOUND_FARGATE_DEPLOYMENTS=1` on the consolidated host so an omitted
container binding fails instead of recreating ECS/RDS/NAT/ALB infrastructure.

The human administrator still selects the exact commit, reviews the plan,
satisfies existing CI and permission checks, approves the release, and sees its
persisted status/history. The new provider sends bounded JSON over SSH to the
fixed root-owned command
`sudo -n /usr/local/sbin/nce-lightsail-release-protocol`. Only `core-worker`
receives its dedicated SSH key; the web process does not receive that key or a
Docker socket. Use a pinned known-hosts file, strict host-key checking, restricted
authorized key with the exact forced command, and sudo permission for that exact
command without arbitrary arguments.

The protocol supports `start`, `recover`, `poll`, `stop` and `credentials`.
Deterministic tokens and exact plan/commit/action receipts preserve the current
two-phase Core worker contract. Recovery checks an existing token and never
resubmits an ambiguous start. The provision phase verifies existing hosting and
its preserved identity contract. Core exports public registration variables and
the private resource bundle before the release phase builds the exact approved
source in isolated on-demand CodeBuild. No application infrastructure is created.
Core then exports its existing resource-credential bundle to a private staged
`core-resource.env`; the release activates it through raw-format Compose env files.

Cancellation also sends the exact approved `plan_hash`. A stop request must
match the original plan, action, commit and binding; omission of that hash is a
transport error and must not be mistaken for an unconfirmed source-build start.

### Existing frontend publisher policy repair

On October 30, the Proposal Studio release's source build and backend migration
succeeded, but its fixed host publisher was denied `CreateDeployment`. CloudTrail
identified the missing resource as the existing Proposal app's
`branches/production/deployments/*` path. The publisher already allowed the two
create/start actions on app, branch and job resources and had the analogous
manual-deployment path for Academy; changing an app's source cannot repair this
operator policy. The first release was recovered with its exact admitted ZIP
and existing operator identity, without another build, migration or restore.

`deployment/repair_frontend_publisher_policy.py` plans only one added statement:
`amplify:CreateDeployment` and `amplify:StartDeployment` on
`arn:aws:amplify:us-east-1:015454941623:apps/d1f14xk20wy3vy/branches/production/deployments/*`.
It preserves every existing statement and cannot select another user, policy,
app, branch, account, action or resource. Planning and apply require the existing
authorized `axolotl-deploy` profile; no credentials enter source or output.
Inherited AWS credential, credential-file and endpoint environment overrides are
removed before invoking that profile; the normal operator's profile files remain
available through the existing local AWS CLI configuration.

```sh
python deployment/repair_frontend_publisher_policy.py plan --plan /private/operator/publisher-plan.json
# Review the complete policy and exact before/after hashes before applying.
python deployment/repair_frontend_publisher_policy.py apply --plan /private/operator/publisher-plan.json --receipt /private/operator/publisher-apply.json
```

Apply refuses a changed before policy and exclusively records a private intent
before one update. CLI retries are disabled. An unknown update requires an
operator read/reconciliation; rerunning apply cannot silently repeat it. This
adds no services, capacity, subscriptions, frontend targets or source-build
authority. The [Amplify service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amplify.html)
describes the actions and branch scope; retain the actual CloudTrail resource
denial alongside the reviewed exact-resource repair, since the service evaluated
the more specific manual-deployment path in this request.

Normal approved releases stage automatically. The fixed root helper
`/usr/local/sbin/nce-lightsail-stage-artifacts` starts only the configured artifact
project, whose trusted builder is pinned to a versioned S3 bundle and checksummed
buildspec. Its separate GitHub checkout must equal the administrator-selected
40-character commit and preserve the existing identity/frontend contract. Fixed
Dockerfiles build AMD64 images; the API/worker share one image and the browser
remains separate. No application database or Core credentials enter CodeBuild.

The root independently reconstructs the Git commit/tree/blob object hashes from
the tracked-source archive, including ignored export attributes and symlinks,
and requires the administrator-approved commit before admitting any build output.
It never extracts or executes that archive on the production host.
The root verifies CodeBuild project, service role, source bundle version,
buildspec, compute/image, secondary repository/commit, build ID/ARN, output
manifest and artifact SHA256 before pulling exact ECR digests. A separate build
lock prevents duplicate starts; a durable intent without a confirmed build ID
requires reconciliation, never an automatic paid retry. Builds occur before
maintenance and the backup/database lock. Existing CI and human approvals apply
before every start; ordinary releases require no operator artifact staging.

On-demand Linux x86 Medium costs $0.01/minute in us-east-1 (verified from AWS
PriceList). The root reserves at most 300 minutes/month, with one build at a time
and a 30-minute per-build limit: at most $3 of this build compute each month.
The dedicated CloudWatch log encryption key adds $1/month plus requests; logs
expire after 14 days. S3/ECR/log usage remains variable. No build fleet, sandbox,
Docker image server or idle build instance is configured. The approved Academy
portability overlay preserves original source separately, accepts only its exact
before/after file hashes and records the patch; an incompatible changed database
bootstrap file requires a reviewed update before deployment.

## Artifact admission and release boundaries

The root-owned `/srv/nce/config/release-bindings.json` contains `version`,
`bindings`, `runtimes`, `releases` and the reviewed `build` contract. `bindings` matches Core's public application
bindings. A runtime has `kind` (`proposal` or `academy`) and its fixed frontend
delivery mode. Fixed service/database mappings are code-owned.
Each staged commit records a `source_sha256`, exact `images` by fixed service
name, and `frontend_sha256`. A runtime's `frontend` is `"amplify"` for the
retained Amplify publisher, `true` for deliberately local delivery, or `false`
for an explicitly backend-only release. Amplify mode additionally pins its
existing `amplify_app_id` and `amplify_branch` (`production`).

The automatic helper writes source at
`/srv/nce/releases/<application-UUID>/<40-character-commit>/source.tar.gz`
and its compiled Amplify artifact at `frontend.zip`, or local static files under
`frontend/` when local delivery is deliberately configured. Source and
frontend hashes must match. Amplify mode uses the ZIP file SHA-256 and requires
`index.html` at its root; traversal, symlinks and environment files are refused.
The local frontend tree digest is SHA-256 of canonical
sorted compact JSON mapping each relative filename to its file SHA-256. Symlinks
are refused. Automatic images are pulled by immutable ECR digest and verified
as AMD64. Explicit operator-staged rollback/recovery entries may also use a local
SHA-256 image ID. Source/browser inputs cannot choose commands, filesystem
paths, ports, service names, secret files or Compose documents.

Before first use, create pinned current image overrides at
`/srv/nce/config/current-proposal.json` and `current-academy.json` for reliable
rollback, and versioned frontend symlinks `/srv/nce/static/proposal` and, where
used, `/srv/nce/static/academy`. Academy's separate owner-only migration runtime
file is `/srv/nce/secrets/academy-migration.env`. Preserve its existing owner URL
and restricted runtime-role bootstrap credentials. Set
`ACADEMY_REUSE_RUNTIME_DB_ROLE=1` only in that migration configuration; the
patched Academy migration verifies the existing restricted role and its password
without granting its database owner role-creation privileges. The normal API
retains its restricted database identity. Core itself is released by
an operator and is outside the two application runtime mappings.

Both application frontends retain their existing Amplify app, production branch,
canonical origins and callbacks. Current bindings are Proposal Studio
`d1f14xk20wy3vy` and Academy `d370756z2vxc0d`. In Amplify mode the runner uses
only `CreateDeployment`, `StartDeployment` and `GetJob` through the fixed
`nce-release` profile at root-only `/etc/nce-release/aws-credentials`. Its IAM
permissions must be restricted to these exact apps/branches and their jobs.
The signed ZIP upload URL remains in memory and is never logged or returned.
The runner waits for `SUCCEED`; an uncertain create/start is recorded for operator
reconciliation and never automatically repeated. These are the documented AWS
[manual upload](https://docs.aws.amazon.com/cli/latest/reference/amplify/create-deployment.html),
[start](https://docs.aws.amazon.com/cli/latest/reference/amplify/start-deployment.html)
and [job status](https://docs.aws.amazon.com/cli/latest/reference/amplify/get-job.html)
interfaces. App creation, CloudFormation, DNS changes and apex migration are
outside this frontend publication path.
Publishing `frontend.zip` changes only the artifact for the pinned existing app
and production branch. It does not call app/branch update or domain-association
APIs, so existing custom-domain associations, SPA/deep-route rewrites, redirects,
response headers and branch settings remain in place. Keep the exact compiled
static root (`index.html`, assets and documentation files at their original
relative paths) in the ZIP; do not add a wrapper directory.

The reverse proxy must return 503 while the corresponding fixed flag exists at
`/srv/nce/maintenance/proposal` or `/srv/nce/maintenance/academy`. The runner
creates maintenance, stops application writers, takes a custom-format database
backup with ACLs preserved, verifies the archive listing, migrates once, starts
the pinned services, and checks readiness/worker/browser health before opening
ingress. It retains job receipts and backups in `/srv/nce/release-state/<token>`.
The runner holds `/run/lock/nce-maintenance.lock` across migration, restart and
any recovery, sharing the backup program's global lock. A contending release
refuses before changing services or maintenance flags. Private Compose
environment strings escape dollar signs for Compose interpolation; the actual
container receives the original credential bytes, including `${...}`, `$word`
and `$$` literals. `deployment/verify_compose_literals.py` verifies this with
synthetic credentials and an isolated container.

If migration fails before new application processes start, the runner restores
the quiesced backup, old pinned services and old frontend. Database owner, exact
locale/provider, database ACL grantors/options, per-role database settings and
connection admission are captured privately and reapplied/compared; recreation
starts with connections disabled until isolation is restored. Once new processes
start, background jobs may already have written data or performed external
actions. A subsequent failure stops writers and keeps maintenance plus the
database for operator recovery; it does not silently roll back or replay those
possible actions. A cancellation lets a running migration finish before its safe
recovery boundary. The initiating administrator can inspect the recorded token;
private logs and credentials are not forwarded into browser receipts.

## Verification and retirement

Run `deployment/lightsail_core_validation.py` through the normal Core management
launcher after restore. It performs read-only deployment checks, verifies no
pending migration, confirms the canonical issuer, counts identities/bindings,
decrypts existing credential ciphertext without exposing it, and records key
fingerprints for comparison. It does not prove a real browser login or PKCE.

Retain full before/after database table hashes, sequences, schema and ACL checks,
object-version/file receipts, matching encryption inputs and an independently
tested restore. Verify canonical HTTPS, discovery/JWKS, cookies/CSRF, real Core
administrator authentication, integrated application PKCE, tenant isolation,
existing employee/family accounts, roles, queued work, integration credential
status, representative authorized file reads and relevant app workflows. Keep
paid model execution and real external message sending out of migration checks.

Only after matching live evidence should ECS services, ALBs, NAT gateways and
RDS be retired. Stopping containers alone leaves networking/database charges.
Review retention/deletion protection first, keep recoverable backups and the
KMS keys they need, and preserve S3/DynamoDB/Lambda/SSM/runner resources still
serving the unchanged static/serverless applications. Document the final resource
inventory and measured ongoing cost separately from the target budget.

## Recorded native rehearsal

The October 27 restored-database rehearsal passed Core's deployment security
checks, reported zero pending migrations, and decrypted seven existing encrypted
credentials across five model-field groups. Eleven identity/binding count groups
were collected. Django, OIDC and origin-boundary key fingerprints matched the
preserved source runtime. Exact bindings were exported from the existing Core
graph without creating identities or starting workers. Private receipts are
`~/.local/share/nce-lightsail-20261028/core-native-validation.json`,
`core-native-validation-proof.json` and `core-native-binding-export.json`.
These are rehearsal receipts; they do not establish final public cutover, real
browser authentication, complete application acceptance or resource retirement.

## Current health and file-bind updates

Core application detail and service inventory display the current Lightsail
provider, preserved application/API endpoints, credential-free live API readiness
and a sanitized root service-health report. Install the fixed eight-service
status publisher and 30-second timer, then mount the status directory read-only
into Core web. Reports older than 120 seconds show unknown health; historical AWS
release references stay in the audit history. The web process receives no Docker
socket or SSH key. Full installation, scoped build policies and encrypted backup
paths are recorded in the migration workspace `AUTOMATIC-BUILDS.md`.

Replacing an individually bind-mounted Caddyfile or Core bindings file atomically
changes the host inode while the container may retain the old file. Recreate the
affected container after such replacement and verify both file hashes. Status
and maintenance flags use directory mounts. Strip incoming origin/forwarded
headers before reverse_proxy; wildcard header_up deletes in the same proxy may
erase trusted headers injected there.

Durable `/srv/nce/build-state` reservations/confirmed build IDs and
`/srv/nce/release-state` recovery receipts belong in protected encrypted backups.
Exclude temporary `/srv/nce/build-cache` downloads and the regenerable status
receipt. A download failure resumes the recorded build token rather than starting
a new paid build; an operator must retain that exact token during recovery.
