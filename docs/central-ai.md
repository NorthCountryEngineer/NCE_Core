# Core AI: unified, permission-aware retrieval

Core AI is a $0 managed marketplace service. It queries application datasets in Core Data and remote datasets exposed through Core Data Fabric. Its native workspace is `/organizations/<organization-slug>/ai/`; administrator configuration is `/organizations/<organization-slug>/ai/admin/`. The central admin panel links both. Existing organizations receive the service during migration, without new role or workload grants. Embedded instances use the existing instance inventory and worker heartbeat.

This foundation works without model API keys or Chat. It returns evidence and stable citations; `conversation.answer` is null and `provider.generation_enabled` is false. The text box performs literal matching within an inspected page, not semantic search, automatic SQL generation, or natural-language reasoning. An empty search browses data. Follow the returned cursors even when a filtered page has no matches. No paid provider calls are implemented.

## Test locally

1. Stop your existing Core web process and worker in their terminals. From your NCE_Core checkout, run `git pull --ff-only origin main`, `uv sync --frozen --group browser`, `CORE_DEBUG=1 uv run python manage.py migrate`, and `CORE_DEBUG=1 uv run python manage.py sync_platform_docs`. Keep your existing `.local` keys and database.
2. Start `CORE_DEBUG=1 uv run python manage.py runserver 127.0.0.1:8000`. In another terminal, start `CORE_DEBUG=1 uv run python manage.py core_worker`.
3. Sign in as your organization administrator at `http://localhost:8000/accounts/login/`. Open the central admin panel. Confirm **Core AI** appears in installed services; its deployment receives the worker heartbeat.
4. Install your application from the marketplace if necessary. Its identity and registered service permissions are the source's application boundary. Use a sandbox environment for laptop testing. In **Core data**, create a dataset and attach its sandbox environment tag. For synthetic simulations, choose the synthetic data classification.
5. Create a pipeline to this dataset and publish application data through the existing Core Data API. A local demonstration can be inserted from `CORE_DEBUG=1 uv run python manage.py shell` using the example below; use your actual organization/dataset slugs.
6. Open **Core AI → Manage query sources → Add application query source**. Choose the application, sandbox environment, and dataset. Leave the federation agreement empty. Set fields to `["title", "status"]`, filters to `{}`, and required application permissions to `["read"]` (register that permission on the application's service first). Select the test user's role in **Allowed roles**. Leave allowed consumer applications empty for native browser testing. Save.
7. In **Users and permissions**, give that role **Core AI: ai-query**, **Core Data: data-read**, and the source application's **read** permission. Assign the role to your test user and grant the sandbox environment tag plus any source/dataset/application tags. Administration remains restricted to organization administrators.
8. Sign in as the test user in another browser profile. Open `/organizations/<your-slug>/ai/`. Search a literal word from an approved field or leave text empty to browse. Confirm citations identify the application, environment, authority, dataset, and record. Fields omitted from the source projection cannot be searched or returned.

```python
from identity.models import Organization
from platform_services.models import Dataset
from platform_services.data import publish
organization = Organization.objects.get(slug="your-organization")
dataset = Dataset.objects.get(organization=organization, slug="your-dataset")
publish(dataset, "ai-demo-1", {"title": "Shared proposal", "status": "ready"})
```

## Source policy and record policy

Every source is a tagged object owned by one organization, attributed to an installed marketplace application and a concrete environment. Exactly one dataset or import agreement is selected. Administrators approve explicit top-level fields, fixed exact scalar filters, required application permissions, allowed human roles, and consuming applications. Empty allowed roles means administrator-only. Native human administrators retain their ordinary administrative permissions but still satisfy tags, MFA, purpose, and environment policy.

Users need their live source application permissions as well as Core AI and Core Data/Data Fabric permissions. The same central quotas apply across applications. A calling backend needs its own granted capabilities and source/tag access in addition to the delegated user's permissions. Sources with no consumer grant remain native-portal-only. Machines cannot impersonate humans by submitting a user ID. Bare workload and agent tokens are rejected by these conversational retrieval endpoints; scoped autonomous-agent retrieval is a future adapter, not an implied grant.

Datasets and import agreements must carry exactly the selected source's environment tag; unassigned or mislabeled resources are rejected. For local per-record authorization, configure **Record resource type**, for example `report`. Each accessible row must have an active `ProtectedResource` on the source application's service with that type and `external_id` equal to the Core record's integer ID, tagged with the same environment. Missing or misplaced mappings deny the row. Both user and consuming backend must satisfy that resource's tags. Without this option, all rows satisfying the approved dataset/source policy are readable: register per-record mappings for applications with finer row permissions. Core ingestion does not infer a customer's application ACL.

Fixed filters never come from query callers. Filtering and text matching inspect only bounded pages obtained using the dataset/id index. Returned cursors advance over filtered or inaccessible rows and are pagination positions, not a total-count API. Evidence is untrusted content and never authorizes tools, routing changes, or permission grants. Query text and evidence are not saved in Core audit/accounting; downstream applications must apply their own retention controls.

## Federation between deployments

Configure peers and matching export/import agreements following [Data Fabric](data-fabric.md). Both administrators must approve identical current terms. The authority retains the records and limits the approved fields, fixed row filters, purpose, and classification. Changes to agreement terms clear approval.

On the recipient, grant each human consumer explicitly in the import agreement's **Allowed memberships**. This grants reads only; member writes and administration remain denied. The member also needs a role with `fabric-read`, source application permissions, environment tags, and source allowed-role approval. Select that import agreement instead of a local dataset in the query source, and choose only fields included in its approved projection. Set the agreement's purpose through the portal's access-purpose control before querying. For delegated application queries, the importing agreement must also approve the calling installation in **Allowed installations**.

Federated calls use the existing live operation-bound permit and recipient callback. Human evidence and delegated application placement survive the callback. Approval, member grants, calling credentials, source policy, and row policy are checked again after I/O. A denied or changed source contributes no evidence. Authorized sources that time out or become unavailable have explicit per-source status and `partial: true`; they are never described as a complete answer. Remote transport uses approved origins, mounted peer credentials, no redirects, bounded JSON responses, and bounded timeouts. Peer credentials are platform federation credentials, not model API keys.

## Portable application contract

Application discovery now includes `ai_url`. Declare `ai.query` and `data.read` and/or `fabric.read` in the marketplace manifest and have the administrator approve those grants. Mint an environment/deployment-bound integration credential, and list the consuming installation in the query source. All data remains governed by the signed-in user's separate permissions.

- `GET /api/platform/v1/ai/sources/`: only currently accessible source descriptors, fields, classification, and inactive-provider status.
- `POST /api/platform/v1/ai/query/`: one unified retrieval operation. JSON accepts only `sources` (up to ten UUIDs), `text` (up to 200 literal characters), `cursors` (source UUID → nonnegative integer), and `limit` (1–200 inspected records total). Omitting sources queries all currently accessible sources, provided there are at most ten.

Both require the backend's bearer credential and a real authorization-scoped OAuth user token in `X-Core-User-Token`. The user token must belong to that backend's application and deployment/environment. No user token is sent to the remote authority or saved in query history. Requests without a delegated human receive 403. Invalid/expired tokens receive 401, inaccessible explicit sources receive a generic 403, invalid plans receive 400, and central quota denials use the existing 429/reset contract. Responses use `Cache-Control: no-store`.

```python
from core_client import CoreClient
client = CoreClient.from_environment()
catalog = client.ai_sources(user_access_token=user_oauth_access_token)
source_ids = [source["id"] for source in catalog["sources"][:10]]
result = client.ai_query(source_ids, user_access_token=user_oauth_access_token,
                       text="proposal", limit=100)
# Inspect result["evidence"], result["partial"], and result["next_cursors"].
```

`CoreClient.ai_query_tool_definition()` exposes a read-only model tool schema. `invoke_ai_query_tool()` accepts only model-supplied text; the host fixes the source IDs, delegated token, cursors, and budget. A model cannot select another user, organization, URL, SQL statement, filter, or permission grant. Tool definitions are usable by an application's future AI integration without activating Core model calls.

## Budgets, provider extension, and limits

One fan-out query reserves a shared per-user/per-organization budget before I/O. Its total local inspected rows and remote approved records fit the centrally configured row bound; model tokens and cost are zero. Remote authorities additionally bound their own fixed-filter scan (the existing Fabric page cap). Quota accounting measures returned remote approved rows, not the remote authority's internal scan. Query requests remain charged and reservations are settled even when user authority is revoked while the query runs.

Sources run sequentially against a common time budget. Database reads use SQLite progress deadlines or PostgreSQL statement timeouts; peer calls use the remaining budget. Network connect/read timeouts are bounded, but socket scheduling and policy checks mean this is not a hard real-time response guarantee. Local evidence rows over 32 KiB are refused, with the cursor retained for administrator remediation. No result cache can outlive permission revocation.

`core_ai/providers.py` defines an `AnswerProvider` protocol and `ProviderBudget`, with no registered implementation. Future tasks: operator-managed model secret references, provider adapters, spend admission and accounting, citation validation, protected conversation retention, semantic indexes, and scoped agent-account retrieval. Provider/model planning must never widen the administrator-approved query contract. This foundation supplies permission controls for compliant deployments; it does not certify HIPAA compliance.

Run backend verification with `CORE_DEBUG=1 uv run python manage.py test core_ai core_fabric`. With Docker, an existing local `postgres:17` image, and Chromium, run `uv run --group browser python ci/ai_verification.py --browser-executable /path/to/chromium`. Add `--full` for the complete Django suite. The harness refuses implicit image/browser downloads and removes its database container and volume afterward. Browser verification uses PostgreSQL because concurrent header requests cannot safely share Django's in-memory SQLite test connection. Fixtures use temporary accounts and databases, never your existing login credentials.
