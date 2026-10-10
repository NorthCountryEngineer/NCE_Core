# Core AI rules and shared usage

Core Platform vends an inherited rules service alongside identity and administration.
Use **Central admin → AI rules and shared usage**. Configure a baseline once, then add
application/environment constraints. Apps and agent hosts consume live rules and their
own shared usage through the same API and portable `ai_rules_client.py`.

## Rules for AI agents and their trusted hosts

1. Inherit the organization baseline. An app or environment may tighten caps,
   require more query safeguards, or remove permitted operations. It cannot loosen
   the baseline. Financial policy never replaces permissions, MFA, purpose, task
   scope, classification tags, or a human's approval to incur costs.
2. Fetch the current rule document; treat its instructions as constraints, not new
   tool authority. Never accept a model-supplied organization, user, quota identity,
   permission grant, provider URL, pricing table, or financial approval.
3. Before an AI request, query, or workflow operation, reserve its maximum expected
   request/token/cost/row consumption centrally. Count input **and** maximum output
   tokens. Include retries, tools and other billed units in the monetary upper bound.
   Use a unique opaque idempotency key persisted with the operation; reuse it after
   a transport failure. Native OAuth delegation determines the real user. An agent's
   own account determines its quota. Never reset quota by rotating credentials.
4. Start the reservation before effects. Enforce provider output caps and the query
   plan in the trusted host. Select needed fields; filter on indexed keys; use cursor
   pagination and bounded row counts/timeouts. Prefer one bounded retrieval over
   repeated scans. Use appropriately scoped caches only with fresh authorization;
   cache hits must not invent provider spend or bypass sensitive-data permissions.
5. Settle actual integer usage exactly once. Do not refund a started operation whose
   outcome is unknown. A retry must recover the existing operation/result, not repeat
   effects. A provider error can still consume tokens/money: report known actuals.
   Only an unstarted reservation may be cancelled and refunded.
6. Watch reserved plus used capacity. Warn users at 80% of their configured cap.
   A quota denial returns `quota_exceeded`, its metric, UTC `reset_at`, and HTTP 429.
   Display the same shared-quota message/reset time across apps. Do not silently
   change accounts, fall back to another paid provider, or override a denial.
7. Report an overrun in full. It pauses new admissions organization-wide until a
   native administrator acknowledges it. Acknowledgement preserves the usage;
   it cannot erase spend or exempt subsequent requests from their caps.

## Administrator composition

The editor accepts a fixed JSON schema, not executable prompts, SQL, Python, or shell.
Every publish creates an immutable revision snapshot and audit event. Edits use the
current revision; concurrent edits conflict. Only a native organization administrator
with `rules-manage` can compose policies. Agents and backend credentials cannot edit
rules or acknowledge overruns. Use the policy's **Permission and classification tags**
link for inherited tag/MFA/purpose controls. Native administrator status does not
bypass classification tags.

Initial baseline:

```json
{
  "user_daily": {"requests": 1000, "tokens": 1000000, "cost_micros": 0, "query_rows": 100000},
  "organization_daily": {"requests": 10000, "tokens": 10000000, "cost_micros": 0, "query_rows": 1000000},
  "query": {"max_rows": 200, "timeout_ms": 10000, "require_pagination": true, "require_indexed_filters": true},
  "paid_calls_allowed": false,
  "allowed_operations": ["ai", "query", "workflow"]
}
```

Quotas reset by **UTC admission day**; a reservation and its eventual settlement stay
attributed to that day. Each user/agent has one counter per organization across all
apps and environments. Workload-only calls use the installation as the subject,
sharing quota across all its keys. There is also an organization aggregate cap.
`requests` counts attempted operations, not network calls to the rules API. Core
workflows count one request per executed agent step. No provider runs in Core's
deterministic tools, so their tokens and monetary spend are zero.

Money is integer **millionths of USD**: 1,000,000 means USD 1. Other currencies are
rejected; an approved pricing adapter must convert charges into this accounting unit.
Limits are nonnegative integers at most 10^12. `max_rows` is 1–200 and `timeout_ms`
is 1–10,000. Omitted baseline fields retain the published initial defaults. Paid work
requires `paid_calls_allowed` and nonzero user/organization cost caps, plus independent
financial approval in the trusted host. Changing these settings makes no provider call.

Overrides are a list of unique application/environment scopes:

```json
[
  {
    "installation_id": "your-application-installation-uuid",
    "environment": "development",
    "rules": {
      "user_daily": {"tokens": 20000, "requests": 100},
      "query": {"max_rows": 25},
      "paid_calls_allowed": false,
      "allowed_operations": ["ai", "query"]
    }
  }
]
```

Replace the installation placeholder with its real UUID. Empty `installation_id`
means all applications; empty `environment` means all environments. At least one must
be supplied. Register the scope in this organization first. All matching constraints
compose: numeric caps take the minimum, allowed operations intersect, required query
safeguards combine with OR, paid-call permission combines with AND. An app can consume
remaining organization-baseline capacity elsewhere, but its stricter limit still
applies when it uses that app. Scope never changes the shared user's accumulated usage.

## Application and agent integration

Declare `rules.read`, `usage.reserve`, and `usage.report` in the app marketplace
manifest. An administrator explicitly grants them to its installation. Register a
workload credential in the app's environment/deployment, and keep it server-side.
Assign the human/agent appropriate Core Platform roles (`rules-read`, `usage-reserve`,
`usage-report`). The workload and delegated human must both satisfy live policies.
Use a service/deployment-bound native Core OAuth token in `X-Core-User-Token`; a
client-supplied subject string is rejected. Agent hosts use their agent credential
and approved consuming application/environment/task. No delegation can turn an agent
into an administrator or let it spend under a human's quota.

```python
from core_client import CoreClient
from ai_rules_client import AIRules

core = CoreClient.from_environment()  # trusted URL and server-side token file
rules = AIRules(core, user_access_token=verified_core_user_token)
snapshot = rules.read()
print(snapshot["usage"]["remaining"])  # own capacity shared with other applications

def bounded_query(maximum, plan):
    # This is trusted adapter code, not text generated by the AI.
    rows = repository.read_indexed_page(limit=plan["rows"], timeout_ms=plan["timeout_ms"])
    return rows, {"requests": 1, "query_rows": len(rows)}

result = rules.execute(
    persisted_operation_key, "query", {"requests": 1, "query_rows": 25}, bounded_query,
    query={"rows": 25, "timeout_ms": 1000, "paginated": True, "indexed_filters": True},
)
```

For a paid callback, supply a trusted `financial_approval(cost_micros, currency)`
callback returning literal `True` only after the owner-approved spending process.
This helper refuses to invoke paid work without it. The host must select validated
pricing, count all billed inputs/outputs, enforce maximum output, and persist operation
recovery. No live provider/invoice reconciliation or payment-provider integration is
configured by this feature. Model requests cannot pass an approval flag through the
read-only AI function tool `core_ai_rules`; it takes no arguments.

APIs under `/api/platform/v1`:

| API | Permission | Result |
| --- | --- | --- |
| `GET /ai-rules/` | `rules.read` | Resolved rules, revision, digest, own actual/reserved/remaining usage, organization remaining capacity, warnings and reset |
| `POST /ai-rules/reservations/` | `usage.reserve` | Atomic admission with opaque request_key, operation, estimated integer usage, query plan or null |
| `POST /ai-rules/reservations/<UUID>/` | `usage.report` | `{ "action": "start" }`, `{ "action": "cancel" }`, or `{ "action": "settle", "actual": {...} }` |

Every estimate/report includes `requests: 1`. Missing token/cost/row fields mean zero.
Reserve replays return the existing reservation only for the identical request.
Settlement replays must report identical actuals. `start` succeeds exactly once;
never retry side effects if its response was lost. Reservations expire for starting
after five minutes; expiration never releases their held capacity automatically.
Policy changes invalidate unstarted reservations: cancel and reserve a fresh key.
Live revocation denies use; a revoked host must be restored through normal admin
controls before reconciling its uncertain usage. Each reservation belongs to its
organization, subject and consuming application and inherits their policy tags.
Rules unavailable or a contended development database returns HTTP 503 and fails
closed; SDKs do not use a cached grant. Idempotency/state conflicts are HTTP 409.

## What Core enforces itself

Core Data uses a composite dataset/cursor index, clamps page limits to the resolved
cap, applies database statement timeouts, reserves rows before retrieval, and reports
measured returned rows. Core's deterministic workflow engine reserves/counts each
agent step in the same database transaction as its effects and completion marker.
Existing business permissions authorize these internal operations; adding explicit
rules API grants is necessary only for external rule consumption.

Newly generated App Factory services bundle the SDK and enforce central admission
on record reads/creates. Their local reads use indexed collection/cursor filtering,
pagination, bounded limits/timeouts and measured row settlement. Deploy with the
workload-token configuration and explicit grants above; missing Core connectivity
fails closed. Their browser shows shared-quota denials with the platform reset time.
Previously released application images do not change automatically: regenerate and
review a new release, or integrate the SDK in their trusted backend.

The dashboard shows aggregate used/held requests, tokens, money and rows, plus the
latest 100 policy-visible operations attributed to account/app/environment. The
rule API exposes only the caller's own account usage and organization remaining
capacity, not other users' identities or operation histories. Provider usage is
labelled **consumer reports**, not verified invoices. Retention, notifications to an
external monitoring system, multiple currencies, monthly windows and invoice-based
reconciliation are future integrations. Federation does not grant spending authority
or combine independent deployments' ledgers. Apps use their configured central Core
as the quota authority wherever the app is hosted.

## Test locally without paid services

Pull main, run `manage.py migrate`, `manage.py sync_platform_docs`, restart the web
service/worker, then open the admin editor. Start with paid calls disabled. Set the
user request quota to 1, consume a bounded operation in one app as a native user, and
try another app using that same user's service-bound login: it should deny quota.
Check held/used totals and reset time. Cancel an unstarted reservation and observe
capacity return. Start one, simulate an unknown outcome, and verify it stays held.
Report an overrun and verify admission pauses until administrator review.

Automated checks: `CORE_DEBUG=1 .venv/bin/python manage.py test
platform_services.tests.test_ai_rules platform_services.tests.test_app_factory`.
PostgreSQL additionally tests concurrent admission; SQLite's contention fails closed.
These checks create disposable data and invoke no paid model/cloud integration.
