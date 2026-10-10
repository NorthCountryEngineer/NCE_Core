# Core Chat

Core Chat is an organization-scoped marketplace core service hosted by Core
Platform and consumed by its UI, application backends, and agent workflows.
New onboarding installs it with the other core services. The migration installs
it for organizations that already have Core Platform. Its embedded deployment
appears in the central instance inventory and is heartbeated by `core_worker`.
Customers can rename its service through the existing service settings.

Open an organization's **Chat** link to use it. The central admin panel's **Core
Chat** section manages settings, participants, tags, and federated connections.
The browser polls every three seconds and preserves drafts. Every poll rechecks
authorization. Cursor APIs support remote applications without requiring Redis,
a message broker, or WebSocket ingress.

See [Chat testing and deployment](chat-testing.md) for a member creation walkthrough,
Mac update steps, and the disposable PostgreSQL/web/worker/browser test suite.

## Permissions and attribution

Organization roles grant permissions on the `core-chat` service. Applications
declare capabilities in their marketplace manifest and receive only grants
an administrator approves:

| Capability | Purpose |
| --- | --- |
| `chat.read` | List joined conversations and read permitted messages. |
| `chat.send` | Send as the authenticated user or application. |
| `chat.manage` | Create conversations; owners manage participants. |
| `chat.ai` | Send explicitly attributed AI messages. |
| `chat.federate` | Attest sender provenance from an approved Core instance. |

Conversation participation is an additional boundary. Participants are existing
organization users or marketplace installations, with owner, member, or reader
roles. A reader cannot send; an owner can manage participants. Organization
administrators manage conversation controls but must explicitly join before
reading messages. These management actions are audited.

Permission tags cover the core installation/service, conversations, participants,
individual messages, and federation connections. Tags inherit from parents and
the organization. Workloads require explicit tag grants and `allow_workloads`;
MFA/fresh-human-authentication tags deny machine access. Delegation checks both
application and user policies, so a human-only tag also prevents service
delegation through that conversation. Human users can use the native UI or a
chat-service OAuth client with verified authentication. No administrator, agent,
or federated peer bypasses these checks. These controls do not establish HIPAA
compliance.

Senders come from authentication. Clients cannot submit a local user ID to
impersonate someone. Messages expose `actor_kind`, `actor_id`, `agent_name`,
`origin_issuer`, and `via_installation_id`. Delegation retains the real user and
calling application. AI is labeled `ai`; peer senders are labeled `federated_user`,
`federated_application`, or `federated_ai`. A remote user ID is an assertion from
that peer, not a local account or a grant of local authorization.

Reads, sends, delegated calls, and permission changes write audit metadata without
message text or credentials. Message APIs expose append/read operations. This
release does not add edits, deletion, attachments, retention, legal holds, or
end-to-end encryption. Configure encryption of databases/backups, network
controls, and audit archival for the deployment's requirements.

## Consume from an application

Add `core-chat` to a package's dependencies and required `chat.*` capabilities to
its manifest. Approve grants in the central panel and add the installation as a
participant in the intended conversations. Existing marketplace deployments
configure `CoreClient.from_environment()` with the Core URL, organization ID,
and mounted workload credential. Discovery advertises `chat_url` and `core-chat`.

```python
from core_client import CoreClient

core = CoreClient.from_environment()
rooms = core.chat_conversations()
page = core.chat_messages(conversation_id, after=0, limit=100)
sent = core.chat_send(conversation_id, request_id, "Your review is requested.")
```

The host supplies a stable request ID per logical send. Retrying returns the
same message; reuse with different content returns HTTP 409. IDs are scoped to
the conversation and sender. Text is nonempty and limited to 16 KiB in UTF-8.
Pages contain at most 200 messages. `next_cursor` advances over scanned rows,
including inaccessible message-level tags. Begin again at zero to revisit
history after tag grants change. PostgreSQL serializes authoritative room writes
so cursor order follows committed messages within that conversation.

For a user's action, pass `user_access_token` to `chat_messages` or `chat_send`.
The token must have `authorization` scope, belong to this application's service,
and retain live user membership. Both user and application must be authorized
participants. Keep tokens/credentials on trusted backends. Native bearer user
clients use an OAuth client registered under `core-chat`, also with
`authorization` scope. Authentication can use any configured upstream provider.

Routes beneath `/api/platform/v1/chat`:

| Method and route | Behavior |
| --- | --- |
| `GET /conversations/` | List up to 200 joined, authorized conversations. |
| `POST /conversations/create/` | Create with `title` and optional `ai_enabled`; creator becomes an owner. |
| `GET /conversations/{id}/messages/?after=0&limit=100` | Read a cursor page. |
| `POST /conversations/{id}/send/` | Send `request_id`, `text`, optional `ai` and `agent_name`. |
| `POST /conversations/{id}/participants/` | Owner/admin updates `user_id` or `installation_id`, `role`, `allow_ai`, and `active`. |

API authentication is a bearer workload credential or chat-service OAuth token.
Delegation uses workload bearer plus `X-Core-User-Token`. `X-Core-Purpose` carries
a configured access-purpose code. Browser forms use sessions and CSRF protection.
Responses use `Cache-Control: no-store`; SDK message methods check the returned
organization and conversation binding.

## AI conversation tools

Enabling AI does not activate a model, charge an account, or send a message.
AI senders also need `chat.send`, `chat.ai`, participation, `allow_ai` on that
participant, and all applicable tags. A model host can read authorized history
and offer the SDK's JSON function schema to its selected provider:

```python
tool_schema = core.chat_tool_definition()
result = core.invoke_chat_tool(
    {"text": proposed_message},
    conversation_id=host_selected_conversation,
    request_id=stable_tool_call_id,
    agent_name="support-assistant",
)
```

The model supplies only text. The trusted host chooses the destination, request
ID, and agent name. Additional model arguments fail before any network call.
Treat chat content as untrusted input; it cannot grant permissions or change
routing. This adapter does not call an external model. Provider credentials,
budgets, approvals, and paid execution belong to the approved model host.

Core Agents provides `chat_send` with this configuration:

```json
{
  "conversation_id": "11223344-5566-4788-9900-aabbccddeeff",
  "text_field": "reply"
}
```

Choose an actual AI-enabled conversation in the agent's organization. Workflow
JSON supplies the configured text field. Rule agents can select this tool
alongside inspect/select/publish. All possible branches need permissions at
submission. The worker rechecks before every step, labels the message with the
agent slug, and uses a stable run/step ID to avoid duplicates after retries.
Revoking AI participation, capabilities, tags, or service availability prevents
queued sends.

## Share between federated Core instances

Each shared conversation has one authoritative Core instance. Other instances
attach local conversations and relay reads/sends over verified HTTPS. Messages
are stored at the authority; consumers keep no local database replica. This
avoids delayed revocation and conflicting histories. Outages return HTTP 503;
retry sends with the same request ID. Connectivity is required. This release
does not provide offline delivery, a background outbox, multi-master replication,
or end-to-end encryption.

1. Choose the authoritative organization and create its conversation.
2. Register a dedicated peer application there, declaring/granting `chat.read`,
   `chat.send`, and `chat.federate`, plus `chat.ai` when required. Add the
   installation as a participant with `federated_issuer` set to the consuming
   instance's exact issuer. Enable `allow_ai` if needed.
3. Issue a peer workload credential. Mount it on the consuming instance beneath
   `/run/secrets/chat-federation/<local-organization-uuid>/peer.credential`.
   Store only its path in Core; never enter its value in the portal or Git.
4. Operators approve exact HTTPS origins on both instances through comma-separated
   `CORE_CHAT_FEDERATION_ORIGINS`. The authority approves the consuming issuer;
   the consumer approves the authority issuer. HTTP is restricted to explicit
   development with `CORE_DEBUG=1` and loopback hosts.
5. In the consumer's Chat administration, create a connection with the authority
   issuer, remote organization UUID, and credential path. Create a conversation
   selecting that connection and remote conversation UUID. Approve local
   participants, tags, and AI independently.

Both instances check live capabilities, participation, tags, and purposes on each
operation. The authority checks the peer's issuer binding and records its
installation as the attesting principal. Responses must match the configured
remote organization, conversation, and issuer. Redirects are rejected, sizes and
timeouts are bounded, credentials reject tenant/symlink escapes, and TLS
verification remains enabled. Targets must be authoritative; chained relays are
rejected to prevent routing loops.

Kubernetes CI/CD configuration supports `chat_federation_secrets`, using the same
`secret_name`, `organization_id`, `key`, and `filename` fields as provider secret
mounts. It creates read-only file `subPath` mounts and named secret references in
CI RBAC/preflight. Put approved origins in
`kubernetes.extra_environment.CORE_CHAT_FEDERATION_ORIGINS`. Secrets must already
exist; the pipeline creates no credentials or paid infrastructure. Coordinate
rotation with credential revocation and workload restarts because `subPath`
mounts do not refresh in running pods.

## Validation

```sh
CORE_DEBUG=1 uv run python manage.py test identity.tests platform_services.tests core_chat.tests
.venv/bin/python -m unittest discover -s ci/tests -v
```

The suite exercises two actual HTTP Core instances with separate processes,
settings, databases, and organizations. It checks shared message IDs, AI
attribution, idempotency, remote binding, and revocation on each side. Test
credentials are generated at runtime and discarded with the fixtures.
