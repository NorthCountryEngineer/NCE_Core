# Agent accounts and marketplace permissions

Every agent definition has its own UUID account in its organization, separate
from human users and application credentials. Agents never inherit their
submitter's authority. Both identities must remain authorized for a queued job.

## Administrator setup

1. Install a template from **Marketplace → Agents**, or create a custom agent.
   Installation creates an account with no permissions or consumption approvals.
   Platform operators curate listings in Django administration; organization
   administrators manage their installed agents in **Agent accounts**.
2. Create organization roles with individual service permissions. An inspector
   needs `agents-run` on Core Agents. A publisher also needs `data-write` on Core
   Data. Chat agents need `chat-send` and `chat-ai` on Core Chat. The reserved
   organization `admin` capability is human-only.
3. Open the account and **Approve application and service roles**. Choose the
   consuming installation, environment, roles, and access-tag grants. Sandbox, development,
   staging, UAT, and production require separate approvals. Approval boundaries
   are immutable; revoke an approval and create another to change its scope.
4. For UAT, approve a task with its application, environment, acceptance criteria,
   and expiration. Set its explicit resource scope using UUIDs from tag pages:

   ```json
   {"datasets": [], "conversations": [], "service_resources": []}
   ```

   Empty lists deny data/chat/registered-resource access. `service_resources`
   refers to the existing registered application resources. Tasks must match their
   approvals' organization, application, and environment. Task revocation and
   expiration immediately block further agent actions.
5. Add chat agents as **agent account** participants, enable AI for the
   conversation, and approve AI for that participant. Application/user membership
   does not grant membership to their agents.

Accounts, approvals, tasks, credentials, roles, service permissions, and resources
support permission tags. Agents need explicit grants and `allow_workloads` on
applicable tags. MFA/fresh-human-authentication requirements deny agent access;
agents cannot borrow human MFA. Purpose restrictions use `X-Core-Purpose`.
Consuming application and task tags also constrain every agent action.

## Remote applications and AI runners

Any application's trusted host uses its integration credential to exchange an
approved binding for an agent credential:

`POST /api/platform/v1/agent-credentials/exchange/`

```json
{"binding_id": "approved-binding-uuid", "expires_in": 900}
```

The caller needs `agents.run` and must be the approved consuming installation.
The opaque Bearer token belongs to the agent with a fixed environment and task.
Core stores only its hash. Credentials last 60–3600 seconds, default 15 minutes,
and expire no later than their task. The admin panel supports one-time credential
display and revocation. Agents cannot mint credentials for other agents.
This has OAuth-style response fields but is a Core service endpoint, not an
additional grant on the human OIDC authorization endpoint.

```python
from core_client import CoreClient

host = CoreClient.from_environment()
agent = host.agent_client(binding_id, environment="uat", task_id=task_id)
run = agent.run_agent("inspector", request_id, {"sample": 42})
agent.invoke_chat_tool({"text": "Please review the acceptance results."},
    conversation_id=conversation_id, request_id=message_id, agent_name="Inspector")
```

The trusted host supplies destination, scope, and request IDs for model tool
calls. `AgentClient.from_environment()` alternatively reads `CORE_AGENT_TOKEN_FILE`,
`CORE_AGENT_ENVIRONMENT`, optional `CORE_AGENT_TASK_ID`, and the normal Core URL
and organization configuration. Store tokens in deployment secret stores, never
Git, model prompts, or messages. Hosts can exchange fresh tokens while their
approvals remain live. No model provider or paid infrastructure is activated.

## Authorization consumed by sub-services

The receiving service authenticates with its own integration credential to
`POST /api/platform/v1/agent-credentials/introspect/`. Send the agent token,
trusted `environment`, and `task_id` (null outside a task). For protected actions,
also send `permission` and optional `resource_type`/`resource_id` identifying an
existing registered resource. Unknown resources and insufficient grants deny
access. Registered resources in UAT must also appear in the task allowlist.

A successful response supplies `principal_kind: agent`, account/binding IDs,
`sub: urn:core:agent:<UUID>`, Core issuer, organization, the receiving service's
audience, expiration, environment, task, and that service's current roles and
permissions. Invalid, expired, revoked, foreign, or insufficiently authorized
credentials return `active: false`.

```python
claims = service_client.agent_authorization(incoming_agent_token,
    issuer=configured_core_issuer, audience=configured_service_audience,
    environment="uat", task_id=trusted_task_id, permission="test-execute",
    resource_type="test-case", resource_id=opaque_fixture_id)
if not claims.get("active"):
    raise PermissionError("Agent action denied")
```

The client validates issuer, audience, organization, subject, expiration, and task.
The receiving service enforces the operation and its own application semantics.
Use HTTPS, fail closed when Core is unavailable, and avoid caching decisions when
immediate revocation matters. Human login retains native/OIDC/Keycloak/cloud
provider support; external agent runners use this account exchange contract.

## Durable workflows and existing agents

Run submissions accept `environment` (default `development`) and optional
`task_id`. Admin-submitted workflows consume agents through Core Workflows;
admin-submitted single agents through Core Agents. A task selects its approved
consuming application for admin submissions. Application submissions use
that application's installation. Approve every agent for the appropriate consumer.
Agent-token submissions retain their credential's environment and task.

The queue pins agent account and approval IDs with tool snapshots. Before each
step it rechecks the submitter and all participating agents' accounts, definitions,
listings, roles, tags, consumers, task scopes, and expiration. Step traces record
agent UUIDs. Audit events and AI chat retain the agent's identity, including across
approved federation. Audit records exclude submitted records and message bodies.

The migration creates accounts for existing definitions without granting roles.
Old queued/running jobs without approved accounts become failed; submit new jobs
after approval. Existing tool configurations remain available. Listing versions
are snapshotted on installation; global edits do not silently change installed
tools. Disabling a listing revokes its installed agents. Built-in tools remain
deterministic; model-backed runners can consume approved services with AgentClient.
These controls prepare for further compliance work and do not certify HIPAA
compliance or implement application-specific UAT acceptance logic.

Environment-bound hosts can exchange credentials only for matching agent
approvals. Add the matching environment boundary tag to the agent's resources;
access-tag grants cannot override deployment environment isolation. See
[modernization.md](modernization.md).

In an organization with registered deployment environments, register an active
environment before approving agents there, including UAT when it needs its own
namespace. Missing or disabled environments deny credential use.
