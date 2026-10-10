# Composable development operations

Open **Central admin → Development operations**. The workspace belongs to the
marketplace's **Core Workflows** service and inherits Core identity, environment
isolation, permission tags, agent accounts, and the central AI usage ledger. No
model provider, network connection, paid training job, or deployment is started
by creating, publishing, importing, or running these recipes.

This release executes bounded local experiments synchronously. It supports data
connections, transformations, data fusions, numeric regression training with
holdout evaluation, prediction, approved agent steps, and nested cross-application
workflows. Long-running distributed training, GPU jobs, remote connector fetching,
model serving deployments, arbitrary scripts, and paid model orchestration are
not execution adapters in this release. Existing durable Core agent workflows
and the synthetic training harness remain available separately.

## Start testing

Create an organization and a sandbox environment through onboarding, then use an
existing administrator account. To create a working synthetic ML example locally:

```sh
cd /workspace/NCE_Core
CORE_DEBUG=1 .venv/bin/python manage.py development_demo \
  --organization YOUR_ORGANIZATION_SLUG --administrator YOUR_ADMIN_EMAIL
```

The command creates 50 synthetic records with `y = 3*x + 2`, a dataset connection,
a training model, and a workflow that composes the two. It publishes those three
demo revisions for review in the workspace; it does not run the experiment or
grant permissions to applications or agents. It only supports sandbox/development
environments. Repeating it verifies existing demo recipes instead of overwriting
them. Run the workflow with a fresh request key and `{}` inputs. Its output is a
model artifact with coefficients, intercept, test MSE/MAE/R², baseline MSE, and
the exact recipe and data digests. Inspect its pinned lineage in Recent experiments.

For a record-only example, create a **pipeline** owned by Core Workflows in your
sandbox, using the initial transform recipe. Review and publish revision 1, then
run it with inputs `{"rows":[{"id":1,"value":42,"extra":"removed"}]}`. The result
contains only `id` and `value`. Editing produces a new draft and leaves revision
1 and existing workflow pins intact.

## Object contract

Every object has an organization, immutable owner/application, environment, kind,
slug, active state, and an explicit list of approved consuming installations.
Every revision has an immutable recipe, SHA-256 digest, author account, dependency
index, resource references, inherited classification tags, and review state.
Published revisions may be withdrawn. There is no implicit “latest” execution:
graphs, SDK execution, and experiments pin revision UUIDs and digests.

Recipes have exactly `operator`, `inputs` (port/type map), and `config`. Ports are
typed `rows`, `model`, or `connection`; edges must match. Graphs reject cycles,
missing nodes, disconnected branches, missing ports, corrupt pins, environment
mixing, and unpublished dependencies. Limits: 24 nodes per graph, eight nesting
levels, 48 expanded operators, eight external ports, 200 records per intermediate,
64 flat scalar fields per record, 64 KiB per recipe, and 1 MiB per final output.
Large joins fail rather than silently truncating. Dataset connections may be
clamped by the organization's lower query limit; their measured query usage is
included in central quotas. Initial dataset reads use the indexed `(dataset,id)`
cursor order. This release reads one bounded batch, not an entire large dataset.
Externally supplied records inherit the destination object's controls. Choose an
appropriately classified object for uploads; uploaded scalar values do not carry
an independent claim about their source or classification.

| Kind | Operator | Inputs | Exact config keys | Output |
| --- | --- | --- | --- | --- |
| connection | dataset | `{}` | dataset_id (UUID), schema_digest, limit (1–200) | rows |
| connection | integration | `{}` | integration_id (application binding UUID) | connection |
| pipeline | transform | `{"rows":"rows"}` | fields (selected names), rename (map), where (scalar equality map) | rows |
| pipeline | pipeline | `{"rows":"rows"}` | pipeline_id (integer), transforms_digest | rows |
| fusion | union | `{"left":"rows","right":"rows"}` | `{}` | rows |
| fusion | join | `{"left":"rows","right":"rows"}` | left_key, right_key, right_prefix | rows |
| model | linear_regression | `{"rows":"rows"}` | features (1–8), target, seed, epochs (20–500), ridge (0–1) | model |
| prediction | predict | `{"rows":"rows","model":"model"}` | field (new prediction field name) | rows |
| agent | agent | `{"rows":"rows"}` | agent_id (integer), binding_id (UUID), definition_digest | rows |
| workflow | graph | declared typed external ports | nodes, output | selected node's type |

Dataset/schema and existing pipeline/agent definition digests are available in the
local Django shell using `platform_services.documentation.checksum`. A changed
definition requires a newly reviewed recipe. The integration adapter resolves the
existing permission-checked application integration descriptor; it does not fetch
remote data or return backend credentials. Agent adapters currently permit only
the bounded `inspect` and `select` tools. The binding must approve that account
for the consuming application/environment/task; its live role and all graph
resource/tag restrictions apply to the data it receives. AI provider calls and
arbitrary generated executable code are not accepted as recipe operators.

Training requires at least ten finite numeric records. A deterministic 80/20
split holds test rows out of fitting and feature scaling. Gradient descent uses
bounded epochs and optional ridge regularization. Model artifacts store numeric
coefficients and evaluation metrics, never executable serialized Python. Model
ports enter through pinned recipe compositions; callers cannot inject model or
connection artifacts as external experiment inputs. Metrics are evidence for an
experiment, not automatic approval to deploy or use a model clinically.

Example graph after publishing a dataset connection and a model recipe:

```json
{
  "operator": "graph",
  "inputs": {},
  "config": {
    "nodes": [
      {"name": "source", "revision_id": "CONNECTION_REVISION_UUID", "digest": "CONNECTION_SHA256", "inputs": {}},
      {"name": "train", "revision_id": "MODEL_REVISION_UUID", "digest": "MODEL_SHA256", "inputs": {"rows": "source"}}
    ],
    "output": "train"
  }
}
```

Graph edges name a node; external edges use `$port_name`. Nodes may reference
published workflow revisions to compose nested workflows. Union and join can
combine outputs owned by different applications when **every referenced object**
is explicitly shared with the consumer and every underlying resource is allowed.

## Sharing and permissions

`development.read`, `development.write`, and `development.run` are Core Workflows
permissions. Registering them during migration grants no new workload or agent
capabilities. Native administrators can review, share, import, withdraw, and disable
objects. Writers, including scoped agent accounts, can propose immutable drafts
for their owning application; only the author or native administrator reads a draft.
Permission-tagged roles and service permissions are checked live. Machine callers
also need explicit marketplace capability grants; `data.read` and `agents.run`
are required when those adapters are used.

Sharing a parent workflow does not share its dependencies or grant data access.
Approve each dependency for the consumer, assign appropriate roles/tags, and use
credentials bound to the correct environment. A workload with delegated user proof
must pass **both** backend and user checks; revocation is immediate on reads,
new runs, and idempotent replays. Run outputs and lineage remain protected by the
entire source resource chain. Previously downloaded data cannot be recalled.
Agent task resource scopes now include `development_objects` UUID lists, in addition
to dataset, integration, and other existing scopes. Empty scopes deny access.

Cross-organization data continues through bilateral Data Fabric contracts. These
recipes cannot bypass fabric scopes or authorize peers. Portable bundles can move
recipes between organizations; they omit records, model-run artifacts, credentials,
tags, sharing grants, and review approvals. Resource references are placeholders.
Import requires explicit local bindings, creates new object/revision identities,
and re-pins selected local definitions. All imported revisions remain drafts, and
dependencies must be reviewed before their parent workflow. A bundle checksum
checks integrity, not publisher trust or permission.

For import, submit this wrapper in the workspace (use a downloaded bundle object):
`{"bundle": BUNDLE, "bindings": {"dataset_id:SOURCE_UUID": "LOCAL_DATASET_UUID"},
"installation_id": "LOCAL_OWNER_UUID", "environment_id": "LOCAL_ENVIRONMENT_UUID",
"prefix": "imported-experiment"}`. Include all resource placeholders from the bundle.
Pipeline/agent binding IDs use their documented integer/UUID types.

## Application and AI integration

The integration document now advertises `development_url`. APIs under
`/api/platform/v1/development/` accept the existing workload or agent bearer token,
optional `X-Core-User-Token`, and the existing purpose header:

| Method/path | Operation |
| --- | --- |
| GET objects/ | Permission-filtered published catalog; kind, limit 1–50, after cursor |
| POST objects/ | Create an unreviewed object; slug, name, kind, installation_id, environment_id, spec |
| POST objects/{object UUID}/revisions/ | New draft with optimistic revision number and spec |
| GET revisions/{revision UUID}/ | Live authorized pinned recipe; optional format=bundle |
| POST revisions/{revision UUID}/runs/ | Execute reviewed recipe with request_key and inputs |
| GET runs/{run UUID}/ | Protected result, metrics, digests, and agent-attributed lineage |

```python
from core_client import CoreClient
from development_client import Development

development = Development(CoreClient.from_environment(), user_access_token=current_user_token)
catalog = development.objects(kind="workflow")
recipe = catalog["objects"][0]
experiment = development.execute(recipe["revision_id"], digest=recipe["digest"],
                                 request_key=host_request_id, inputs={})
model = experiment["result"]["value"]
```

Use `AgentClient` for an agent's own credentials. `Development.tools()` exposes a
read-only catalog tool and a draft-proposal tool. `invoke_tool` takes the object
UUID and expected revision from the host; model-generated recipe text cannot
choose another object, publish it, share it, authorize paid calls, or deploy it.
The host invokes `execute` only after choosing a reviewed revision and digest.

Experiments commit bounded local computation, result lineage, and central request/
query accounting together. Every operator consumes an operation request; dataset
reads also consume a query request and actual returned rows; agent steps additionally
consume their account's operation request. Repeating the same request key, consumer,
account, revision, and input returns the original result without re-executing.
Changed input/revision with that key returns 409; quota rejection returns the shared
429/reset contract. Failed local experiments roll back their outputs and reservations.
Permission and quota rejections produce denial audits. Admin Recent experiments shows
completed results; rejected executions are in audit events, not persisted partial
model runs. No remote side effects are performed inside this transaction.
