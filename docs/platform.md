# Organizations, core services, and application deployments

Core Platform is the default name. `CORE_PRODUCT_NAME` changes it for a deployment,
and Organization settings allow each customer to choose its own display name.
Immutable organization and service UUIDs and the trusted issuer establish authority.

## Developer onboarding

An operator creates an account and grants its developer designation in platform
account administration or through `manage.py grant_developer <email>`. This is a
server-side privilege. Development configuration or a sign-in parameter never
grants it. A new developer's browser sign-in leads into `/onboarding/`:

1. Create an organization and choose its name, slug, and product display name.
2. Supply legal name, country, support address, timezone, billing address, currency,
   and preferred future payment provider.
3. Choose development/staging/production, an existing Kubernetes namespace,
   application origin, optional ingress class/TLS secret, and automatic deployment.
4. Register a runtime connector and save its one-time key, or defer connection when
   automatic deployment is disabled.
5. Review and activate the four core marketplace services. Automatic deployment
   requires a connected runtime; no cluster or infrastructure purchase is made.

The wizard persists progress and creates the first organization administrator.
They assign the remaining user roles, application capabilities, and configuration
from the central panel. Lost runtime keys can be replaced during onboarding,
revoking the previous key. Subsequent administrator keys are issued once through
the panel and can be revoked. Users can belong to multiple organizations.

Billing preferences use provider-neutral adapters for Stripe, Paddle, Adyen, and
Braintree. Every adapter deliberately rejects checkout creation until a real
integration is implemented. No payment methods, charges, subscriptions, or paid
model calls are created by this application.

## Marketplace and permissions

Database migrations seed four built-in packages: `core-platform`, `core-data`,
`core-agents`, and `core-workflows`. They use the same installation and service
registry as applications. Core runtime code executes inside this deployment;
installing a core package activates its organization workspace and creates its
monitored instance. Installing it does not start a separate remote container.

Platform superusers publish application packages in `/platform-admin/`. Example
manifest (replace the illustrative digest with the real registry manifest digest):

```json
{
  "image": "registry.customer.example/reports@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "port": 8080,
  "health_path": "/health",
  "callback_path": "/oauth/callback",
  "dependencies": ["core-data", "core-workflows"],
  "capabilities": ["data.read", "data.write", "agents.run", "workflows.run", "workflows.read"]
}
```

Packages allow only this bounded manifest vocabulary. An organization's
administrator approves the installation's capabilities. Dependencies install
automatically, including the core controller workspace, but declared capabilities
are not automatically granted. Versions
and manifests are snapshotted; publishing a new catalog release cannot change
installed code or permissions. **Review latest release** explicitly approves its
manifest and grants, updates callbacks, and requests a Kubernetes rolling update.
Controller/core runtime image upgrades remain operator release operations.

Human permissions and workload capabilities are distinct. Organization
administrators receive `admin` and registered permissions for their services.
Members receive only permissions assigned through organization roles. Workload
credentials receive the installation's current approved core capabilities and
never impersonate a human administrator. Removing a grant, disabling a service,
disabling its installation/organization, or revoking a key takes effect on the
next API check. Each organization's datasets and agent definitions remain private
to that organization; capabilities currently apply to all its datasets.

## Connect Kubernetes

Use an existing cluster and namespace with ingress routing, DNS, registry access,
and TLS configured by its operator. Production must use HTTPS for both core and
application origins. The connector initiates HTTPS requests to core; the core
does not need inbound access to the cluster. Configure namespace-scoped access:

```sh
uv run python scripts/connector_rbac.py --namespace customer-core > /tmp/core-runtime-rbac.json
kubectl apply -f /tmp/core-runtime-rbac.json
```

Use the `core-runtime` service account in a supervised connector process, with an
operator-provided kubeconfig or in-cluster Kubernetes configuration. The generated
Role permits only that namespace's Deployments, scaling, Services, Secrets, and
Ingresses. It grants no cluster administration or namespace creation. Keep its
Kubernetes credentials separate from the core connection key. Store the panel's
one-time runtime key in a private file, then start:

```sh
uv run python runtime_connector.py \
  --core-url https://core.customer.example \
  --token-file /run/secrets/core-runtime-key \
  --namespace customer-core --context customer-context
```

`kubectl`, Python, and `requests` are required. The connector polls every 15 seconds,
applies only assigned instances, and refuses to overwrite objects owned by another
organization. Integration secrets travel via stdin into Kubernetes Secret objects;
commands and diagnostics omit their values. Normal workloads run as non-root,
without Kubernetes service account tokens, with read-only root filesystems,
bounded CPU/memory, and readiness checks. Application images must support UID
10001 and a writable `/tmp`; this initial manifest does not supply arbitrary
volumes, environment variables, private-registry pull secrets, or custom resources.
Private registries require operator-configured namespace access.

From the central panel, install an application, approve its capabilities, and
**Deploy an instance** onto a connected runtime with its application origin.
With automatic deployment enabled it starts immediately; otherwise it registers
stopped and starts when the administrator chooses **Start**. This uses configured
cluster capacity. **Restart** changes the pod revision. **Stop** scales to zero,
disables that instance's OIDC client, and revokes its access tokens and workload
key. Starting again issues a fresh workload key; other instances remain active.

Instances report the requested revision, observed state, health, and last contact.
Health requires the new Deployment generation and replicas to be ready. Reports
older than 90 seconds show stale status. A core outage retains existing workloads;
it does not claim fresh health or authorize new API requests. Revoking the runtime
connection halts management immediately, so stop workloads first or use the
cluster operator's controls afterwards. Stopped Deployment, Secret, Service, and
Ingress resources remain registered for reuse; deletion/retention is an operator
operation in this first version.

The controller's own core services are included in inventory. **Connect this
controller's Kubernetes deployment** adopts an existing Deployment carrying
`core.platform/organization=<organization-uuid>`, using the configured runtime and
namespace. It monitors readiness and permits rolling restart without replacing
the controller's image or configuration. The controller cannot stop itself.

## Application integration

Marketplace deployment automatically injects:

| Environment variable | Value |
| --- | --- |
| `CORE_ISSUER` | Trusted core issuer origin |
| `CORE_ORGANIZATION_ID` | Installation's organization UUID |
| `CORE_SERVICE_ID` | Its registered service UUID |
| `CORE_OIDC_CLIENT_ID` | This instance's confidential OIDC client ID |
| `CORE_OIDC_CLIENT_SECRET_FILE` | Read-only file holding the client secret |
| `CORE_SERVICES_URL` | Trusted core origin plus `/api/platform/v1` |
| `CORE_WORKLOAD_TOKEN_FILE` | Read-only file holding its workload key |
| `CORE_APPLICATION_ORIGIN` | Configured application origin |

Applications use these values to configure their OIDC library and core service
client. This removes manual registrations and credential copying for compatible
applications; it cannot add authentication code to arbitrary existing binaries.
See [OIDC and central authorization](integration.md) for sign-in and application
administrator enforcement. Keep the client secret and workload key on the backend.

The portable Python client requires only `requests`:

```python
from core_client import CoreClient

core = CoreClient.from_environment()
integration = core.discover()
core.ingest("reports-import", "source-event-123", {"title": "Report"})
page = core.records("reports", after=0, limit=100)
run = core.run_workflow("publish-report", "job-123", {"title": "Report"})
status = core.run_status(run["run_id"])
```

Apps running outside managed Kubernetes use **Create integration key** and configure
the same trusted API URL, organization UUID, and private token file. They can
consume all core APIs over HTTPS without running in the same network/cloud.
Applications in other languages use the same JSON protocol:

| Method and path under `/api/platform/v1` | Required capability |
| --- | --- |
| `GET /integration/` | Active workload key |
| `POST /pipelines/<slug>/events/` | `data.write` |
| `GET /datasets/<slug>/records/?after=0&limit=100` | `data.read` |
| `POST /agents/<slug>/runs/` | `agents.run`, plus capabilities for every possible tool |
| `POST /workflows/<slug>/runs/` | `workflows.run`, plus capabilities for every possible tool |
| `GET /runs/<uuid>/` | Original workload key or `workflows.read` in the same organization |
| `POST /runtime/sync/` | Assigned runtime connection key, not a workload key |

Send `Authorization: Bearer <workload-key>`. Ingestion body is
`{"event_id":"source-event-123","data":{"title":"Report"}}`. Run submissions use
`{"request_id":"job-123","input":{"title":"Report"}}`. Maximum JSON request size
is 256 KiB; record pages contain at most 200 records. IDs are idempotent within
their dataset/organization: the same ID and input returns the prior record/run;
conflicting input returns HTTP 409. Runs return HTTP 202 while queued. Invalid
credentials return 401, missing capabilities 403, invalid requests 400, and
cross-organization resources 404. Network errors and redirects grant no authority.

## Data, agents, and workflows

Administrators create datasets with JSON Schema and pipelines referencing their
own organization's active datasets. Schemas permit local references only. Pipeline
transformations run in order; supported operations are `select` and `rename`:

```json
[{"operation":"rename","fields":{"subject":"title"}},
 {"operation":"select","fields":["title"]}]
```

Agent kinds are `inspect` (no configuration), `select` (`{"fields":["title"]}`),
`publish` (`{"dataset":"reports"}`), and `rules`. A rule agent chooses one of the
approved tools, with no arbitrary code, shell, remote fetch, or paid model:

```json
{
  "rules": [{"field":"approved","equals":true,"tool":"publish",
             "configuration":{"dataset":"approved-reports"}}],
  "default": {"tool":"inspect"}
}
```

Rules evaluate in order, with exact type/value matching and at most ten rules.
Publication datasets must belong to the same organization. Grants cover every
possible tool before enqueueing, even when a particular input chooses inspection.
Workflows list up to 20 agent slugs in execution order and pass each result to the
next agent. Submissions freeze definitions; later edits apply to new runs.

Run `manage.py core_worker` to execute queued jobs. Execution leases recover stale
workers, step results persist transactionally with local data writes, and an event
ID per run/step prevents duplicate publication on retries. The panel displays run
input/output and chosen-tool traces. **Retry run** preserves original input,
snapshot, caller, and completed steps; it cannot lend the administrator's authority
to a revoked workload. Restore the original caller/destination first, or submit a
new run to use edited definitions. External side-effect tools and model-backed
reasoning require additional adapters, credential handling, and billing approval.

## Validation

```sh
CORE_DEBUG=1 uv run python manage.py test identity.tests platform_services.tests
CORE_DEBUG=1 uv run python manage.py makemigrations --check --dry-run
```

The standard suite includes real HTTP client/API integration and transport tests.
The Kubernetes cluster test is skipped unless explicitly configured. For a local
cluster named `nce-platform-test`, load a built image from
`examples/Marketplace.Dockerfile`, identify its actual OCI manifest digest, and run:

```sh
CORE_DEBUG=1 \
NCE_TEST_KUBECONFIG=/path/to/disposable-kind-kubeconfig \
NCE_TEST_KUBECTL=/path/to/kubectl \
NCE_TEST_IMAGE=your-loaded-image@sha256:its-real-manifest-digest \
uv run python manage.py test platform_services.tests.test_kubernetes_cluster
```

The test requires context `kind-nce-platform-test` and an API server at
`https://127.0.0.1:<port>`. It creates and removes its own random namespace and
verifies actual apply, restricted RBAC, mounted integration, HTTP readiness,
rolling restart, upgrade, and scaling to zero. It never creates a hosted cluster.
