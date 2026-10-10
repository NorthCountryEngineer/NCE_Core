# Privacy and international readiness implementation queue

Review completed on 10 October 2026 against commit
`5bb31119a735860ce6fdfcf37bc628819601ec10`.
See the [international regulatory review](../international-regulatory-review.md)
for evidence, jurisdiction scope and source-verification limits.

These are proposed engineering tasks, not implemented capabilities or a legal
certification. The current request authorized review; no retention, deletion,
transfer, consent or customer-data policy was changed. Work should proceed one
task at a time, with reviewable changes and meaningful synthetic-data validation.
Policy object names below are proposed contracts, not existing models/APIs.

Engineering owner: Codex when implementation is requested. Policy/legal owner:
the deploying organization/controller, supported by its operator and appropriate
advisers. Operator facts and legal decisions are prerequisites where identified;
an AI agent cannot create them by assigning a permission tag. Existing controller
procedures can be integrated rather than replaced.

## Sequence and dependencies

| Task | Priority | Dependencies | Deliverable | State |
| --- | --- | --- | --- | --- |
| PRIV-00 | Review | None | Code/data-flow inventory, GDPR gaps, international applicability and official-reference list | Complete |
| PRIV-01 | P0 | PRIV-00; controller policy decisions | Versioned processing-activity and organization privacy policy objects | Queued |
| PRIV-02 | P0 | PRIV-01 | Marketplace application privacy/lifecycle capability contract | Queued |
| PRIV-03 | P0 | PRIV-01–02 | Verified, tenant-scoped subject-rights cases, deadlines and export orchestration | Queued |
| PRIV-04 | P0 | PRIV-01–02 | Retention schedules, legal holds and reviewed disposition plans | Queued |
| PRIV-05 | P0 | PRIV-03–04 | Core/app lifecycle adapters and controlled execution, including backup restore handling | Queued |
| PRIV-06 | P0 | PRIV-01–02; operator/provider evidence | International-transfer approvals and deployment region/egress policy | Queued |
| PRIV-07 | P0 | PRIV-05–06 | Federated lifecycle notices, receipts and reconciliation | Queued |
| PRIV-08 | P1 | PRIV-01–03 | Organization privacy notices, applicable consent/withdrawal and browser-storage inventory | Queued |
| PRIV-09 | P1; P0 for regulated use | PRIV-01–06; intended-use/provider assessment | AI/ML data-use approvals, provenance and lifecycle controls | Queued |
| PRIV-10 | P0 | PRIV-01–07; operator runbooks | Security/privacy incident handling and operational evidence | Queued |
| PRIV-11 | Conditional | PRIV-00; selected markets/sectors | Jurisdiction and commercial deployment packs, current legal verification | Queued |
| PRIV-12 | P0 release verification | Applicable preceding tasks | Cross-app privacy UAT and deployment-specific readiness record | Queued |

P0 means a recommended capability or effective controlled alternative before
ordinary production personal-data rollout. It does not mean every task is a
universal statutory obligation. A manual alternative needs demonstrated coverage,
scope, deadlines, receipts and operator ownership; an unchecked policy document
is insufficient. A feature's legal priority increases when its intended use changes.

## Acceptance criteria

### PRIV-01 — Composable processing and privacy governance

Define `ProcessingActivity` and `OrganizationPrivacyPolicy` contracts for purposes,
data classes, people affected, controller/processor roles, lawful basis, any
special-category condition, recipients, retention justification and contact points.
Attach policies to apps, datasets, chat, workflows, agents and data connections.
Distinguish access-purpose codes from legal grounds. Record reviewed DPIA/DPO/
representative decisions, relevant contractual references and revisions without
putting personal documents or contract secrets in source control. Support applicable
processing-record exports and inherited policies with explicit app obligations.

Acceptance: a synthetic app and dataset have a versioned effective policy; an
administrator can explain inherited requirements and reviewed exceptions. Neither
an agent nor an app can claim legal approval through arbitrary JSON or role grants.

### PRIV-02 — Application privacy capability manifest

Define `PrivacyCapabilityManifest` for subject-reference mapping, data stores,
retention binding, lifecycle/export operations, regions, recipient/processor
references, backup responsibilities and contact/owner. Inventory standalone,
platform-installed, generated and externally integrated apps. Register provider
capabilities, including documented manual procedures when supported automation
does not exist. Pin approved revisions and show unsupported operations to admins.

Acceptance: generated app storage and Core chat declare their actual gaps; claiming
OIDC/RBAC inheritance does not mark lifecycle support complete. Marketplace/admin
views expose reviewed capabilities and missing required actions without exposing
another tenant's inventory. Unverified manifests cannot approve production use.

### PRIV-03 — Subject-rights cases and controlled export

Implement `DataSubjectCase` with proportionate verification, controller scope,
request type, statutory/region-specific deadline, extension notice, assigned owner,
protected evidence, response and receipt. Cover non-user subjects as well as
accounts. Distinguish access from conditional portability; support correction,
restriction, objection and erasure decisions, including justified exceptions.
Use finite-lived protected exports and third-party redaction. AI assistance uses
an explicitly scoped task and cannot approve a denial or expand access.

Acceptance: a user shared between two organizations can receive a complete
per-controller response without seeing the other tenant. Verify non-member records,
mixed-subject chat, disproportionate verification rejection, deadline/extension
handling, revoked operator access, and export expiry. No case creates a broad
tenant data-extraction permission for a UAT agent.

### PRIV-04 — Retention and legal-hold policy

Define versioned `RetentionPolicy` and `LegalHold` bindings, justified durations,
expiry triggers, minimal hold scope, reviewed exceptions and disposition plans.
Include chat, records, revisions, execution results, metrics, audit and generated
app accounting. Account deactivation and credential expiry remain distinct from
content disposition. Preserve required audit integrity while permitting controlled
archival expiry and link retirement; never assume indefinite audit retention is
mandatory. Provide dry-run counts and reviewed plans before destructive execution.

Acceptance: expired synthetic content is identified accurately, holds protect only
their scope, changing an organization's policy cannot purge another tenant, and
repeat runs are idempotent. No arbitrary global 30-day or six-year legal default.

### PRIV-05 — Lifecycle adapters and reliable execution

Implement subject discovery and correction/restriction/disposition adapters across
identity, chat, datasets, workflow histories, documentation versions, model/lineage
stores, app specifications, metrics, audit linkages and application databases.
Resolve protected foreign keys through reviewed retirement/redaction semantics;
do not disable authorization or indiscriminately cascade-delete approvals.
Publish portable contracts for independently deployed apps and generated templates.
Track partial completion, failures, legal holds, and recipient notification duties.
Plan finite backup expiry and restore-time replay with the responsible operator.

Acceptance: remove or restrict a synthetic subject across current and historical
stores, preserve a justified hold and another organization's account, handle a
temporarily unavailable app, resume safely, and prevent restored data from returning
to normal processing. A failed adapter must prevent a false completion receipt.

### PRIV-06 — Deployment geography and international-transfer governance

Define `TransferApproval` and deployment geography facts covering primary storage,
backups, processing, operator/support access, identity providers and subprocessors.
Record the applicable legal mechanism, evidence reference, parties, data classes,
purposes, expiry, and reviewer. This is a technical record of a human/legal decision,
not automatic validation of SCCs or a DPF certification.

Integrate approved routes into Fabric, chat federation, integrations, future model
calls and central metrics. Bind placement requirements to actual provider inventory
and Kubernetes scheduling/egress configurations; labels alone do not prove country
placement. Reassess recipient or location changes and check EU/UK/local mechanisms
separately. Do not assume GDPR forbids all non-EU hosting.

Acceptance: a synthetic cross-border route is denied until its current required
approval is present; expired/changed evidence blocks new processing. A same-region
service with an overseas support recipient cannot silently bypass the assessment.
Document which network/storage paths require operator enforcement beyond Core.

### PRIV-07 — Federated lifecycle events and recipient accountability

Add signed or equivalently authenticated, scope-bound correction, restriction,
erasure-notice and retention events for cooperating apps/peers. Establish safe
subject/record mapping, idempotency, receipts, retries, version reconciliation and
restore tombstones. Separate processor execution from an independent controller's
reviewed response. Reject untrusted events and cross-organization identifiers.

Acceptance: two disposable deployments handle a relevant notification, temporary
peer outage, replay, expired grant and partial refusal without leaking records or
marking all copies deleted. Track delivered exports requiring a manual recipient
procedure; do not promise recall of uncontrolled downloaded files.

### PRIV-08 — Transparency and applicable consent

Provide versioned organization notices and purpose-specific consent receipts only
where consent is the selected valid basis; record withdrawal and reviewed handling
of affected operations. Inventory required/optional browser storage, external logos,
notifications and planned mobile SDKs. Prefer approved same-origin assets. Keep
notification permission, OAuth consent, access grants and privacy consent distinct.

Acceptance: users see the applicable notice and recipient/retention information;
withdrawing an optional purpose does not remove unrelated necessary service access.
Necessary session/CSRF controls are not incorrectly presented as optional tracking.
Accessibility and localization apply to notices and rights interfaces.

### PRIV-09 — AI/ML data-use and regulated-use controls

Define `ModelDataUseApproval` and dataset/model lineage requirements for intended
use, allowed sources, processing purpose, provider/region, no-training/reuse terms,
retention, evaluation, and applicable human oversight. Distinguish synthetic flags
from verified anonymous data. Add preflight checks for source policies and future
provider adapters; assess affected artifacts when records are corrected or erased.
Maintain the current inactive paid-provider default.

Acceptance: a simulated dataset containing a real-data marker is not treated as
automatically anonymous; a provider or incompatible training purpose cannot bypass
approval. A significant-effect decision needs its use-specific assessment and
meaningful human review where required. Demonstrate artifact retirement or a
documented limitation rather than claiming unproven perfect machine unlearning.

### PRIV-10 — Breach handling and security evidence

Provide scoped incident cases, awareness times, affected stores/regions/recipients,
controller/processor responsibilities, assessment decisions and notification timers.
Support applicable regional thresholds rather than one universal 72-hour rule.
Connect tested backup restoration, encryption/key custody, scoped administration,
vulnerability handling and log filtering evidence. Avoid incident payloads or real
customer data in shared development/CI artifacts.

Acceptance: run a synthetic controller/processor breach exercise, including a
reasoned non-notification case and applicable high-risk subject notification.
Record timeliness and recipients; a simulated exercise never sends real regulatory
notifications or triggers unapproved external-provider actions.

### PRIV-11 — Country and sector deployment packs

For selected markets, verify current regulator texts, scope, commencement dates,
representative/registration requirements, contracts, transfer routes, language,
consumer/tax/payment duties, accessibility and applicable security/product rules.
Add human instructions for necessary provider accounts, legal agreements,
registrations, app-store declarations and any fees. Assess AI Act/NIS2/CRA/Data Act/
DORA/DSA/Online Safety scope based on the actual offering; health, children and
China deployments receive their own review. Record review expiry and owner.

Acceptance: a deployment pack states what applies, what does not and why, which
facts are unverified, and required human actions. It never declares universal
worldwide compliance or purchases/contacts/registers anything for the user.

### PRIV-12 — Privacy UAT and release evidence

Create meaningful end-to-end synthetic scenarios for access, correction, deletion,
restriction, objection, portability conditions, multi-tenant accounts, holds,
exports, generated apps, federated copies, revoked permissions, deadlines,
provider changes and restoration. Use role/task-limited test agents and finite
fixtures. Test human-facing rights flows for keyboard/assistive-technology access.

Acceptance: publish a deployment-specific capability/evidence record listing
remaining manual processes, unsupported stores and responsible operators. Technical
test results must not masquerade as regulatory certification or a legal opinion.

## Human actions and cost boundary

No paid infrastructure, provider subscription, certification, lawyer engagement,
filing or external account change is part of this queue's review work. GitHub work
can proceed within the user's authorization. If implementation reaches a required
action outside GitHub/NorthCountryEngineer or any cost, prepare concrete human
instructions and stop that action for the user to perform and then hand back.
Do not fabricate contract acceptance, country approvals or regulator notifications.
