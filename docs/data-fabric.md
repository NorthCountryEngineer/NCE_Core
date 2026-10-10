# Core Data Fabric: bilateral platform federation

Core Data Fabric (`core-fabric`) is a consumable marketplace core service with
organization permissions, tags, API discovery, and a monitored embedded instance.
Applications declare it as a dependency and consume it wherever deployed through
their local Core. Existing organizations receive it with empty grants; no peers,
contracts, or sharing permissions are configured automatically.

It provides a virtual view of source-owned Core Data datasets for acquisitions,
partnerships, joint UAT, and agent workflows. Each administrator approves matching
terms, selected fields/rows, read/write scope, purpose, and optional expiration.
Separate contracts enable reciprocal sharing between independently hosted instances.

```mermaid
sequenceDiagram
    participant App as Application or agent
    participant R as Recipient Core
    participant S as Source Core
    App->>R: Read/write approved contract
    R->>R: Check local approval, caller roles/tags/task
    R->>S: Exact request and 60-second operation permit
    S->>S: Check source approval, bridge identity, data policy
    S->>R: Verify permit and current recipient authority
    R-->>S: Exact operation proof and actual actor UUID
    S->>S: Recheck source policy; filter/project or validate/write
    S-->>R: Records or idempotent receipt
    R->>R: Recheck local policy and response binding
    R-->>App: Authoritative result and provenance
```

## Connect two deployments

1. Set distinct HTTPS issuers and organization UUIDs. On both sides, explicitly
   allow the other origin in comma-separated `CORE_FABRIC_ORIGINS`. Only development
   allows HTTP loopback.
2. Install a dedicated application identity for incoming peer calls. Its manifest
   declares `core-fabric` dependency and `fabric.peer`. Source bridges also need
   `data.read` and/or `data.write`; recipient callback bridges need `fabric.peer`.
   An integration identity does not need a deployed container to call the API.
3. Issue credentials in each organization panel and securely exchange them.
   Mount the **other** deployment's token under
   `CORE_FABRIC_SECRET_ROOT/<local-organization-uuid>/peer.credential`. Production
   defaults to `/run/secrets/data-fabric`; development uses ignored `.local/data-fabric`.
   Store no credential values in Git, agreement JSON, prompts, or screenshots.
4. In **Data fabric and federation → Configure peer**, select the dedicated local
   inbound installation, remote issuer/organization, and outbound credential path.
   Peer identities/routing are immutable. Rotate credentials through mounted
   files and revoke old application keys.
5. Source administrator selects **Propose data sharing**: dataset, explicit field
   list, exact scalar equality row filters, purpose, expiration, read/write scope.
   Writes default off; future fields are excluded from the explicit list.
6. Copy the displayed terms to the recipient administrator. They select **Import
   peer agreement**, choose the peer, and explicitly allow consumer installations
   and agent bindings. Importing does not approve the agreement.
7. Both administrators review and approve the same fingerprint. Approval requires
   a CSRF-protected human administrator request; workloads/agents cannot approve.
8. Grant consuming applications `fabric.read` / `fabric.write`, and agent roles
   `fabric-read` / `fabric-write` on Core Data Fabric. Send the agreement's exact
   purpose. UAT tasks must allow the local import UUID in `fabric_contracts`.
9. Revoke on either side to stop further operations. Both instances audit approval
   and federation decision metadata, without record bodies.

The fingerprint binds agreement UUID, source/recipient issuer and organization
UUIDs, source dataset UUID/classification, fields, filters, purpose, permissions,
and expiration. Editing terms clears approval; both sides must approve matching
new terms. Dataset and relationship routing are immutable. Cosmetic names do not
change identity. Consumer allowlists remain local administrator policy; they do
not grant application capabilities, agent roles, task scope, or tag access.

Every operation has a 60-second permit bound to its digest, caller identity,
operation, and terms. Source callbacks verify current recipient approval and
caller authority. A peer credential alone cannot bypass this. Permits store
hashes/identity references, are deleted after operations, and are limited to 100
concurrent operations per organization. Both sides recheck policy after network
I/O. Peer outages fail closed; there is no offline approval cache.

Demoting/disabling an approving administrator, disabling a peer/core service,
expiring terms/tasks, changing tags, or revoking caller credentials/roles stops
access. Grant source/recipient tags explicitly to dedicated bridges and local
consumers. MFA/fresh-human-authentication tags deny machine federation.
Already committed source writes remain after revocation.

For two local deployments, use separate checkouts/databases, `.local` files, and
ports. Set `CORE_ISSUER=http://localhost:8000` on one and
`CORE_ISSUER=http://localhost:8001` on the other; run its server on 127.0.0.1:8001.
Run migrations in each checkout. The same credential and approval steps apply.

## Application and AI clients

```python
from core_client import CoreClient

client = CoreClient.from_environment()  # CORE_PURPOSE_CODE matches the agreement
agreements = client.fabric_contracts()
page = client.fabric_records(import_contract_id, after=0, limit=100)
receipt = client.fabric_publish(import_contract_id, host_event_id,
    {"project": "joint", "title": "Shared report"})

tools = agent.fabric_tool_definitions()  # agent has its own approved AgentClient
page = agent.invoke_fabric_tool("core_fabric_read", {"limit": 25},
    contract_id=host_selected_contract_id)
receipt = agent.invoke_fabric_tool("core_fabric_publish", {"data": proposed_data},
    contract_id=host_selected_contract_id, event_id=host_event_id)
```

The trusted host fixes contract/event identifiers. Model arguments cannot choose
peers, issuers, organizations, credentials, purposes, or identities. All operations
use live agent roles/approval/task boundaries. No model provider is activated.

Reads scan up to 1,000 source records and return at most 100 approved records per
bounded page. `next_cursor` is the last scanned record, so an empty filtered page
can still advance: continue until the cursor stops. Source-owned records retain
synthetic flags, training IDs, and federation provenance. Reads do not replicate
records into the recipient database. Writes contain only approved fields, match
row scope, and satisfy the current source schema. Event IDs are idempotent within
agreement and verified actor identity. Provenance records recipient issuer/org,
actual actor UUID, purpose, and fingerprint. JSON equality distinguishes booleans
from integers in row filters and replay checks.

| Path relative to `/api/platform/v1` | Capability |
| --- | --- |
| `GET /fabric/contracts/` | `fabric.read`, allowed consumer and purpose |
| `GET /fabric/contracts/<local UUID>/records/` | `fabric.read` |
| `POST /fabric/contracts/<local UUID>/events/` | `fabric.write` |
| `POST /fabric/exports/<agreement UUID>/read/` | dedicated bridge, `fabric.peer` + `data.read`, verified permit |
| `POST /fabric/exports/<agreement UUID>/write/` | dedicated bridge, `fabric.peer` + `data.write`, verified permit |
| `POST /fabric/permits/<UUID>/verify/` | dedicated bridge, `fabric.peer` |

Kubernetes/CI supports `kubernetes.fabric_secrets` entries (`secret_name`,
`organization_id`, `key`, `filename`) and
`kubernetes.extra_environment.CORE_FABRIC_ORIGINS`. Existing secret references are
mounted read-only under tenant paths on web, worker, and migration jobs. Adapters
do not provision clusters, create secret values, or activate paid services.

This first fabric exposes Core Data datasets with their authoritative storage,
schema, and tags. External SQL/object-store connectors, durable replication,
offline delivery, conflict merging, and ownership transfer require additional
adapters. Chat federation retains its existing conversation authority protocol.
These controls alone do not establish HIPAA certification.

Run `CORE_DEBUG=1 uv run python manage.py test core_fabric.tests` for configuration,
permission, permit, scope, provenance, and real two-deployment HTTP tests with
administrator approval/revocation on both portals. [local-testing.md](local-testing.md)
walks through running and testing the platform yourself.

Environment-bound consumers and bridge keys need matching local environment
boundary tags on fabric agreements and their datasets. Export agreements inherit
the dataset environment; assign an import agreement its recipient environment
tag explicitly. Existing bilateral approval and purpose checks remain required.
