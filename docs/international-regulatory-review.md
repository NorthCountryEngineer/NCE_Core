# International privacy and regulatory readiness review

Review date: 10 October 2026. Source baseline: commit
`5bb31119a735860ce6fdfcf37bc628819601ec10`. Scope: repository code, deployment
templates, generated applications, and documentation. No customer database,
deployed infrastructure, contracts, or provider accounts were inspected.

## Assessment

NCE Core can be hosted outside the US and integrate with regional OIDC providers.
No mandatory US hosting or enabled external generative-model dependency was found.
Its tenant boundaries, live permissions, permission tags, constrained queries, and
bilateral federation approvals support privacy engineering. The current product
does **not provide a complete GDPR privacy lifecycle by default**. In particular,
subject rights, finite retention, coordinated deletion, and legally governed
international transfers need additional implementation and operator processes.

This review does not certify compliance or establish that a particular deployment
is unlawful. Some obligations can be met through effective, documented manual
procedures; GDPR does not require a particular self-service portal. Actual
readiness depends on each controller's purposes, people affected, hosting and
support locations, contracts, and operational evidence. Ordinary permission
approvals do not establish a lawful processing basis or a transfer mechanism.

The EU/EEA and UK are the first recommended readiness targets. China and regulated
health, finance, employment, or children's deployments need separate assessments
before launch. A $0 marketplace listing does not remove privacy obligations.

**Source limitation:** public HTTPS requests to EUR-Lex, the European Commission,
and ICO failed through the workspace proxy. Official references below identify
the governing texts and authorities, but their live text was not retrieved.
Current amendments, commencement schedules, adequacy decisions, local rules,
and registration requirements must be verified for the intended launch date.
This is particularly material for the EU AI Act, Cyber Resilience Act, UK reforms,
and India's phased data-protection regime. This review is an engineering
assessment, not a current legal opinion from those authorities.

## Personal data and system boundaries

An identifier remains personal data when it can be linked to a person. UUIDs,
pseudonymous sender IDs, activity counters, and audit references do not become
anonymous merely because names are omitted. Application JSON, documents, chat,
and model inputs can also contain special-category or other sensitive data.

| Store or flow | Data and affected people | Evidence and lifecycle implications |
| --- | --- | --- |
| Accounts, memberships, invitations | Email, names, password hashes, organization roles, identity-provider subject, sign-in evidence; users and invitees | [Identity models](../identity/models.py); one global account may belong to multiple organizations. Invitation/token expiry does not define retention of all associated records. |
| OIDC and application authorization | Subject, organization, roles, permissions, authentication context | [OAuth validator](../identity/oauth.py), [identity views](../identity/views.py); email/name are scope gated, and introspection omits them. Downstream claims and application logs still need their own controls. |
| Organization and billing setup | Legal name, support/billing email, country, provider reference | [Platform models](../platform_services/models.py); payment checkout is currently disabled without a configured approved provider. Billing contact data still counts as personal data. |
| Chat | Message text, sender identifiers, topic, participation, read state, notification settings | [Chat models](../core_chat/models.py); content can mention non-users. Protected relationships and no implemented message deletion/retention workflow complicate erasure. |
| Datasets and execution | Arbitrary JSON records, workflow inputs/outputs, snapshots, errors, feedback, lineage | [Platform models](../platform_services/models.py), [development models](../platform_services/development_models.py); no general subject locator or scheduled content-retention policy was found. |
| Documentation and app creation | Markdown, immutable revisions, descriptions, specifications, approval references, exported indexes | [Documentation models](../platform_services/documentation_models.py), [App Factory models](../platform_services/app_factory_models.py); changing the latest page does not erase its history or delivered exports. |
| Security and AI accounting | Stable subject IDs, decisions, purpose codes, usage totals, request references, timestamps | [Audit models](../identity/models.py#L202), [usage models](../platform_services/rule_models.py); useful minimal metadata, but still linkable. Audit deletion is blocked at the ORM, with no implemented finite purge process. |
| Application-owned storage | Business records and subject-linked accounting, including generated services | [Generated server](../platform_services/app_factory_templates/server.py); its reference SQLite database defaults to `/tmp/app.sqlite3`. Ephemeral loss is not a managed retention or erasure policy. |
| External systems and copies | Federated chat/data, identity-provider records, exports, browser asset requests; operator logs and backups | [Fabric terms](../core_fabric/services.py#L30), [chat federation](../core_chat/federation.py), [header assets](../platform_services/assets/header.js#L215); legal responsibility and lifecycle extend beyond Core's database. |

The main flows are browser → Core → Core database; application ↔ Core identity,
authorization, data and metrics; Core ↔ approved identity provider; and Core ↔
approved peer deployments. Federation and exports may deliver copies even where
the authoritative record remains at its source. An administrator-configured
external logo is fetched by the browser and reveals network/browser metadata to
that host. Logs, backups, CI artifacts, and support access are additional
operator-controlled paths. No active generative-model provider was found.

## Safeguards already present

- Organization-scoped memberships and service roles, live resource permission
  tags, verified authentication evidence, and bounded delegated agent permissions
  provide access controls. Resource context must still be supplied by consuming
  applications. See [identity access](../identity/access.py) and
  [security configuration](security.md).
- OIDC scope filtering limits email/name disclosure; exact registered redirects,
  PKCE, token expiry/revocation, secure production cookies, CSRF protection, and
  HTTPS production settings support authentication security. These settings do
  not attest to the security of an actual deployment.
- AI retrieval uses approved fields, filters, source/row permissions, and bounded
  queries. Query text and evidence rows are excluded from its audit/accounting
  records. [Core AI](../core_ai/services.py#L183) currently returns retrieved
  evidence; the [model provider](../core_ai/providers.py#L26) is inactive.
- Fabric approves explicit fields, purposes, operations, and expiry on both
  deployments; outbound connections use allowlisted HTTPS with TLS verification.
  Revocation blocks new authorized operations. It cannot recall copies already
  delivered to another organization.
- Audit events omit tokens and record payloads. Desktop chat notifications use
  generic text instead of message contents. Integration credentials receive
  [Fernet encryption](../platform_services/secrets.py), but this is not encryption
  of all business data, a per-tenant key system, or proof of encrypted backups.
- Training runs require synthetic-class datasets and exclude production
  execution. This limits exposure but does not prove anonymization: simulation
  constants can contain real personal data, and the synthetic flag follows the
  dataset classification. See [training generators](../platform_services/training_plan.py)
  and [record publication](../platform_services/data.py#L29).

## GDPR and UK GDPR gaps

Priorities below are release recommendations for ordinary production personal
data, rather than claims that every feature is required by every law. The
[implementation queue](tasks/privacy-compliance.md) records dependencies and
acceptance criteria. References to GDPR articles use [the official regulation][gdpr].

### G1 — Processing accountability and lawful purposes: priority 0

No structured inventory of processing activities, lawful bases, controller and
processor responsibilities, special-category conditions, recipients, or retention
justification was found. Permission tags and a caller's `purpose_code` describe
authorization, not an Article 6 basis or an Article 9 condition. OAuth scope
approval is not a record of GDPR processing consent.

Establish controller/processor roles per activity; an organization will commonly
control its application data while a platform operator may process it and control
its own billing/security activities. These roles depend on actual decisions and
contracts. Maintain applicable Article 30 records, notices under Articles 13/14,
Article 28 processor terms and subprocessor governance, and Article 26 arrangements
where purposes and means are jointly determined. Assess DPIA requirements
(Article 35), DPO requirements (Article 37), and non-EU representative requirements
(Article 27), including their exceptions. A small organization is not automatically
exempt from processing records or the rest of GDPR.

Employee monitoring, sensitive records, datasets about people outside the user
directory, and subsequent AI reuse need their own assessments. Use contract or
another applicable basis when appropriate; do not impose blanket consent as the
universal basis for necessary account or workplace processing.

### G2 — Subject rights across organizations and applications: priority 0

There is no complete subject-rights case workflow or export across identity,
chat, datasets, revisions, execution history, and application-owned storage.
[Security audit export](../identity/management/commands/export_security_audit.py)
is an operator audit tool, not a complete access or portability response.
`PROTECT` references include chat participants and approval/evidence records;
removing a user row is neither a reliable erasure operation nor a complete result.

Provide verified, controller-scoped access, correction, erasure, restriction,
objection, and applicable portability handling (Articles 15–22). Article 15 access
is broader than Article 20 portability; portability has specific legal-basis and
automated-processing conditions and does not cover every inferred output.
Responses must protect other people's information. Erasure has exceptions,
including applicable legal obligations and legal claims; a denial or partial
response needs an assessed reason, rather than a permanent technical obstruction.

Under Article 12, respond without undue delay and normally within one calendar
month; a permitted extension of up to two further months requires timely notice
and reasons. Account identity verification should be proportionate and should not
collect unnecessary identity documents. One organization's case must not expose
another tenant's records or delete a global user's other memberships.

An effective manual procedure can meet these duties before a portal exists, but
it needs a complete inventory, protected exports, deadlines, decision records,
and working per-store actions. An AI assistant may support a scoped case; it
should not independently approve exceptions or broaden subject access.

### G3 — Retention, legal holds, and recoverable deletion: priority 0

Chat messages, arbitrary records, workflow results, documentation revisions,
model artifacts, app specifications, and subject-linked metrics lack an integrated
finite retention mechanism. [Append-only audit models](../identity/models.py#L202)
block ordinary updates/deletes and organization cascades; stable actor IDs remain
personal data. Token, permit, task, and invitation expiry controls authorization
validity, not the retention of all related personal data. Deactivation is not
erasure.

Define justified schedules by processing purpose and data class, with scoped legal
holds and controlled deletion/redaction. GDPR has no single mandatory retention
period for all platform data. Security or statutory retention can justify selected
records for a defined period, not indefinite retention of every message or actor.
Keep audit integrity while enabling approved archival expiry and pseudonym-link
retirement where appropriate; do not wipe evidence indiscriminately.

Include replicas, documentation exports, search indexes, derived data/model
artifacts, downstream applications, operator logs, and backups. For backups that
cannot be individually edited, document finite expiry, isolation from ordinary
processing, and restore-time deletion/restriction replay. Test that restoring a
backup does not resurrect erased data into normal service.

### G4 — Cross-border processing and residency controls: priority 0

[Deployment environments](../platform_services/models.py#L15) record namespaces
and origins, not storage, backup, support, or processing countries.
[Organization profile country](../platform_services/models.py#L49) is metadata.
[Kubernetes manifests](../platform_services/kubernetes.py) do not enforce
geographic placement. Fabric's bilateral terms include fields, operations, purpose,
and expiry, but no legal transfer mechanism or geographic destination. Technical
approval does not establish an Article 28 DPA or Chapter V transfer safeguard.

Maintain a verified inventory of actual hosting, storage, backups, subprocessors,
support/administrator access, identity providers, and each external integration.
Record approved destinations and mechanisms and check them before data-bearing
federation, chat, integrations, model calls, or central telemetry. The same applies
when an organization uses a remote parent Core deployment. Review whether a
particular flow is a Chapter V transfer, including separate overseas recipients
and remote support access, rather than assuming that only record export counts.

GDPR does **not** generally require all personal data to remain in the EU.
Transfers can use applicable adequacy decisions or valid Article 46 safeguards.
For EU-to-US transfers, a current, in-scope recipient certification under a valid
EU–US Data Privacy Framework decision may be relevant; merely using a US cloud
vendor is insufficient. Otherwise assess an applicable mechanism such as
SCCs, the destination's law and practices, and necessary supplementary measures.
UK transfers need the applicable UK route, such as an IDTA or UK Addendum and
transfer assessment, or an applicable UK adequacy route. Verify those separately.
See [EU SCCs][scc], [DPF certification][dpf], and [ICO transfer guidance][ico-transfers].

EU hosting alone does not resolve non-EU recipient/support paths. Conversely,
origin allowlists and encryption alone do not legalize a transfer. Countries and
provider changes need reviewed evidence and expiry/reapproval, not a hostname
heuristic. Sector contracts can require tighter residency than general privacy law.

### G5 — Federation and application lifecycle propagation: priority 0

Federation revocation stops future reads/writes but no general protocol propagates
rectification, restriction, or erasure to copies already received. Core identity
and RBAC inheritance do not make standalone or generated application storage
participate in subject-rights handling. Exported documentation and data cannot
be recalled merely by removing a permission.

Define a marketplace privacy capability contract with subject locators, lifecycle
operations, export scope, retention policy, processing regions, recipient mapping,
and receipts. Add scoped, authenticated lifecycle events and acknowledgement/
reconciliation for cooperating applications and peers. Address GDPR Article 19
recipient notification where applicable. Retain proof of notifications and
independent recipient decisions; do not promise remote destruction of files or
data held by independent controllers that Core cannot control.

### G6 — Notices, consent where needed, and browser integrations: priority 1

No organization-specific privacy notice/version registry or granular processing
consent/withdrawal ledger was found. Browser permission for notifications,
permission tags, and OIDC authorization consent serve different purposes.
Disclose actual activities, retention, recipients, overseas processing, automated
decisions where applicable, and rights contacts. Bind genuinely consent-based
activities to specific purposes and withdrawal; do not make a service unavailable
because someone declines an unnecessary optional processing purpose.

Inventory cookies, session/local storage, SDKs, external assets, and future mobile
push/analytics providers against national ePrivacy implementation and UK PECR.
Strictly necessary session and CSRF controls generally do not require a blanket
opt-in cookie banner. Assess optional storage separately, including any applicable
user-requested-function exemption. An external logo request reveals an IP address
but is not automatically a consent-requiring cookie. Prefer same-origin approved
assets and accurately disclose third-party processing. No general advertising or
analytics SDK was found in the reviewed first-party implementation.

### G7 — Operational security and incident readiness: priority 0

The repository describes production TLS, non-root workloads, mounted secrets,
database options, backups, rate limiting at ingress, and log hygiene in
[deployment guidance](deployment.md). It does not prove they are operating.
PostgreSQL `sslmode=verify-full` is supported but not universally enforced.
Integration credentials are encrypted using a key derived from the Core secret;
business records do not receive general field-level encryption. Actual storage
encryption, key custody/rotation, remote administration, monitoring, recovery,
vulnerability management, and test data isolation require evidence.

Select risk-appropriate Article 32 measures; GDPR does not mandate one particular
encryption architecture for every record. Exercise a breach process that identifies
the controller, processor, affected records, recipients, and relevant authorities.
Article 33 normally gives a controller 72 hours after awareness to notify the
authority unless the breach is unlikely to risk people's rights; reasons for delay
are required. A processor informs its controller without undue delay. Article 34
can require communication to people without undue delay for high risk, subject to
its exceptions. Other jurisdictions have different thresholds and deadlines.
Maintain a protected incident record even for a reasoned non-notification decision.

### G8 — AI, ML, simulations, and new purposes: priority 1; priority 0 for regulated use

The current retrieval service and deterministic agents do not send data to an
enabled generative provider. They still process personal data when their sources
do. Simulation flags alone do not make a dataset anonymous. Linear-model runs,
prediction outputs, revision history, and feedback can preserve personal data.

Before enabling a provider, semantic indexing, or cross-app model training, assess
purpose compatibility/lawful basis, minimization, provider reuse/training terms,
retention, processing regions, subject rights, and security. Establish dataset and
model lineage plus a practical plan for artifacts affected by correction/erasure;
do not promise perfect machine unlearning without evidence.

GDPR Article 22 concerns qualifying **solely automated decisions with legal or
similarly significant effects**, not every algorithm or AI response. Employment,
credit, access to services, or health uses may need additional safeguards and
meaningful human review. The [EU AI Act][ai-act] separately depends on the system's
intended use and provider/deployer role. A configurable marketplace and generated
apps do not eliminate those responsibilities. Review prohibited practices,
literacy, transparency, high-risk use, and general-purpose model obligations where
relevant; confirm operative dates and amendments before enabling those uses.

## International launch matrix

This matrix identifies likely assessment areas, not authorization to launch or a
claim that every law applies extraterritorially to every installation. Sector,
employee, public-sector, and provincial/state rules can supplement these regimes.
Verify current local rules with the named authority for the intended activities.

| Market and authority | Applicability and potential barrier | Required deployment assessment |
| --- | --- | --- |
| EU/EEA — national data-protection authorities; EDPB | GDPR establishment/targeting/monitoring scope; ePrivacy national rules; [GDPR][gdpr], [EDPB][edpb] | G1–G8, controller/processor agreements, rights and finite retention, transfer mechanisms; DPO/representative/DPIA where triggered. EEA and EU rules need applicable incorporation/national context checked. |
| UK — ICO | UK GDPR, Data Protection Act, PECR and commenced reforms under the Data (Use and Access) Act; [ICO][ico] | Separate UK transfer route and notices, UK representative/fee assessment where applicable; verify commenced reforms rather than assuming EU text or dates apply unchanged. |
| Switzerland — FDPIC | Revised FADP; foreign disclosure safeguards, transparency, high-risk assessments; [FADP][fadp] | Swiss recipient/transfer requirements and adapted contract terms; foreign-controller representative where statutory conditions apply. EU GDPR documentation alone is not the entire Swiss assessment. |
| Canada and Québec — OPC; provincial authorities, Québec CAI | PIPEDA and applicable provincial laws; Québec private-sector privacy law; [PIPEDA][pipeda], [Québec law][quebec] | Accountability for foreign processing, notice and contractual safeguards. Québec requires relevant privacy impact assessments, including communication outside Québec, and has specific transparency requirements for exclusively automated decisions. Check French-language and local contract duties where applicable. |
| Brazil — ANPD | LGPD territorial tests include specified Brazil-related processing; [LGPD][lgpd], [ANPD][anpd] | Lawful basis, data-subject service, security/incidents, responsible privacy contact, and applicable ANPD international-transfer instruments. EU SCCs do not automatically substitute for Brazilian clauses. |
| Australia — OAIC | Privacy Act/APPs for covered entities, relevant small-business exceptions and extraterritorial nexus; [OAIC APP guidance][oaic] | APP 8 overseas disclosure/accountability, notices, access/correction, justified disposal, Notifiable Data Breaches assessment; check commenced reforms and sector requirements. |
| Singapore — PDPC | PDPA organizational processing and applicable exemptions; [PDPA][sg-pdpa], [PDPC][pdpc] | DPO/contact, purpose/consent or valid exception, comparable overseas protection, retention and breach-notification duties. Server location alone does not satisfy transfer limitation. |
| South Africa — Information Regulator | POPIA responsible-party/operator conditions and applicable territorial scope; [POPIA][popia] | Registered/designated information-officer duties as applicable, operator agreements, section 72 transfer conditions, security compromise process, sensitive/children's processing and possible prior authorization. |
| Japan — PPC | APPI and relevant overseas processing scope; [PPC legal resources][ppc] | Applicable foreign-transfer basis and information obligations, recipient safeguards, access/correction/discontinuance conditions, breach reporting and representative/contact duties where applicable. |
| South Korea — PIPC | PIPA and relevant overseas services/data transfer rules; [PIPC][pipc] | Correct transfer basis/notices, local-representative conditions, retention/destruction, security and notification rules. Review children's data, sector requirements and relevant amendments. |
| India — MeitY; Data Protection Board framework | DPDP Act and rules/notifications subject to their actual commencement; [MeitY][meity], [Gazette][india-gazette] | Verify what is operative at launch, consent and statutory legitimate uses, notices/rights/grievance, children's rules, significant-data-fiduciary duties if designated, country restrictions and sector localization. Do not import GDPR legitimate interests as an assumed Indian basis. |
| China — CAC and other competent authorities | PIPL, Cybersecurity Law, Data Security Law and applicable cross-border provisions; [CAC][cac], [NPC][npc] | Separate local advice: sensitive-data conditions, overseas recipient obligations, transfer mechanism and current thresholds/exemptions, important data/CIIO obligations, representative where required. Localization is conditional, not universal. Public hosting/distribution may also require ICP, app filing or telecom permissions; private intranet deployment differs. |

## Other regulatory and commercial constraints

| Area | When it matters | Architecture and release implication |
| --- | --- | --- |
| AI regulation | AI offered or used in regulated territories, especially employment, credit, health, education, biometrics or public services | [EU AI Act][ai-act] intended-purpose/role classification, transparency and applicable human-oversight/evidence duties. Evaluate a generated app's actual use; no blanket exemption for deterministic tools or a platform wrapper. Verify phased application and amendments. |
| Cybersecurity obligations | In-scope digital infrastructure, cloud/MSP services, software products or critical-sector suppliers | [NIS2][nis2] depends on activity, size/exceptions and national implementation; [CRA][cra] depends on product scope and role, including whether SaaS is an integral remote-processing solution. Establish vulnerability handling, dependency/SBOM evidence, secure updates and incident reporting where applicable. Verify CRA staged dates; standalone SaaS is not automatically in scope. |
| Financial customers | Regulated financial organizations and their ICT providers | [DORA][dora] and local supervisory requirements can drive audit rights, incident assistance, resilience testing, subcontractor and exit obligations. Ordinary suppliers are not automatically designated critical ICT providers. Account tags cannot replace contractual/operational controls. |
| Switching and data portability | In-scope data-processing services and connected-product/data relationships | [EU Data Act][data-act] may require switching/export assistance and contract provisions; this is distinct from GDPR subject portability. Review service definition, exemptions and applicable timelines before offering hosted Core commercially. |
| Public content, marketplaces and messaging | Public/user-to-user services and qualifying hosting/platform activities | [EU DSA][dsa] and [UK Online Safety Act][uk-osa] have specific scope and exemptions. Assess public marketplace descriptions, chat distribution and user audience; private enterprise chat does not justify assuming every messaging service is excluded. In-scope services may need reports, moderation, appeals, child-risk and transparency controls absent here. |
| Accessibility and consumer services | Covered consumer services such as e-commerce, public-sector delivery, and national disability laws | [European Accessibility Act][eaa] and public-sector/national rules depend on service and exceptions. [Accessibility engineering review](accessibility.md) improves the shared UI but is not certification; device, assistive-technology and human assessments remain. |
| Health, children, employment and public-sector data | Sensitive records, clinical intended purpose, minors, staff monitoring or government contracts | Special-category and children's rules, local health/education law, employment consultation, procurement/residency and assurance requirements can exceed generic GDPR. Medical-device duties depend on intended purpose; generic chat/retrieval is not automatically a medical device. Do not advertise HIPAA controls as an EU/UK health-law authorization. |
| International trade and encryption | US-origin software/technology, restricted destinations/entities/end users, applicable export or import controls | Screen actual distribution/support against [OFAC][ofac] and [BIS][bis] requirements and local rules. Encryption export classification, mass-market/open-source conditions or filings may matter; there is no universal prohibition on international deployment. Privacy consent does not override sanctions. |
| Contracts, tax and payment | Commercial hosting, subscriptions, payments, consumer marketplace sales | Determine VAT/GST, merchant role, local consumer rights, notices/refunds and payment-provider coverage. Billing is currently a placeholder. PCI DSS is a payment-security standard, not a regulator or GDPR certification; keep card data with an approved provider when payments are enabled. |
| Mobile distribution | Public iOS/Android releases | Stores require accurate privacy/data-safety declarations and applicable deletion/support/content controls in addition to law. The [mobile task list](tasks/mobile-delivery.md) queues these; no store filing or approval was performed. Provider/account actions and fees require the user to act. |

SOC 2 reports, ISO 27001 certification, WCAG results, permission tags, and
Kubernetes deployment are useful evidence or engineering controls. None alone is
a legal permission to operate worldwide or a GDPR compliance certification.

## Deployment-specific release evidence

Before ordinary EU/UK production rollout, the controller/operator should record:

1. Activities, lawful bases, privacy contacts, roles, applicable notices and
   contracts; a documented assessment of DPIA/DPO/representative requirements.
2. Actual primary, backup and support locations, recipients and subprocessors;
   applicable transfer safeguards and approved changes. Hostnames and profile
   country are insufficient evidence.
3. A working subject-rights procedure across every deployed application and
   relevant copy, with protected exports, deadline tracking and scoped actions.
4. Justified retention schedules and legal-hold decisions; tested disposal,
   revocation, restoration and downstream notification procedures.
5. Security and breach-response evidence, including backup recovery, log hygiene,
   administrator access, patching and regional notification decision-making.
6. A use-specific assessment before AI providers, regulated automated decisions,
   sensitive sectors, children, public content, new countries or payments are enabled.

Use only clearly synthetic data for development/UAT until these arrangements are
in place. Manual processes may support an initial constrained deployment, but
unsupported application stores and untracked transfers must not be hidden behind
a global claim of platform compliance. Each marketplace app needs its own declared
processing and lifecycle capabilities, inherited policies and tested integration.

Engineering remediation is queued in [Privacy and international readiness tasks](tasks/privacy-compliance.md).
The organization/operator must supply actual legal and operational decisions;
Codex should not invent provider agreements, consent records, registrations or
country approvals. This review changed no runtime policies or customer data and
performed no paid actions, external account changes, or regulatory filings.

## Official references for verification

These references were not live-validated from this workspace. Check the current
consolidated texts, regulator guidance and national implementation before relying
on an operative date, exemption or transfer decision.

- EU/EEA: [GDPR][gdpr], [EDPB][edpb], [ePrivacy Directive][eprivacy],
  [EU SCC decision][scc], [EU–US Data Privacy Framework][dpf].
- UK/Switzerland: [ICO legislation and guidance][ico], [UK transfer guidance][ico-transfers],
  [Swiss FADP][fadp].
- Americas: [Canada PIPEDA][pipeda], [Québec private-sector privacy law][quebec],
  [Brazil LGPD][lgpd], [ANPD][anpd].
- Asia-Pacific/Africa: [OAIC APP guidance][oaic], [Singapore PDPA][sg-pdpa],
  [PDPC][pdpc], [South Africa POPIA][popia], [Japan PPC][ppc], [South Korea PIPC][pipc],
  [India MeitY][meity], [India Gazette][india-gazette], [China CAC][cac], [China NPC][npc].
- Additional EU/UK regimes: [AI Act][ai-act], [NIS2][nis2], [Cyber Resilience Act][cra],
  [DORA][dora], [Data Act][data-act], [DSA][dsa], [European Accessibility Act][eaa],
  [UK Online Safety Act][uk-osa]. US trade: [OFAC][ofac], [BIS][bis].

[gdpr]: https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng
[edpb]: https://www.edpb.europa.eu/
[eprivacy]: https://eur-lex.europa.eu/eli/dir/2002/58/oj/eng
[scc]: https://eur-lex.europa.eu/eli/dec_impl/2021/914/oj/eng
[dpf]: https://www.dataprivacyframework.gov/
[ico]: https://ico.org.uk/for-organisations/
[ico-transfers]: https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/international-transfers/a-guide-to-international-transfers/
[fadp]: https://www.fedlex.admin.ch/eli/cc/2022/491/en
[pipeda]: https://laws-lois.justice.gc.ca/eng/acts/P-8.6/
[quebec]: https://www.legisquebec.gouv.qc.ca/en/document/cs/P-39.1
[lgpd]: https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709.htm
[anpd]: https://www.gov.br/anpd/pt-br
[oaic]: https://www.oaic.gov.au/privacy/australian-privacy-principles/australian-privacy-principles-guidelines
[sg-pdpa]: https://sso.agc.gov.sg/Act/PDPA2012
[pdpc]: https://www.pdpc.gov.sg/
[popia]: https://www.justice.gov.za/legislation/acts/2013-004.pdf
[ppc]: https://www.ppc.go.jp/en/legal/
[pipc]: https://www.pipc.go.kr/eng/
[meity]: https://www.meity.gov.in/
[india-gazette]: https://egazette.gov.in/
[cac]: https://www.cac.gov.cn/
[npc]: https://www.npc.gov.cn/englishnpc/
[ai-act]: https://eur-lex.europa.eu/eli/reg/2024/1689/oj/eng
[nis2]: https://eur-lex.europa.eu/eli/dir/2022/2555/oj/eng
[cra]: https://eur-lex.europa.eu/eli/reg/2024/2847/oj/eng
[dora]: https://eur-lex.europa.eu/eli/reg/2022/2554/oj/eng
[data-act]: https://eur-lex.europa.eu/eli/reg/2023/2854/oj/eng
[dsa]: https://eur-lex.europa.eu/eli/reg/2022/2065/oj/eng
[eaa]: https://eur-lex.europa.eu/eli/dir/2019/882/oj/eng
[uk-osa]: https://www.legislation.gov.uk/ukpga/2023/50/contents
[ofac]: https://ofac.treasury.gov/
[bis]: https://www.bis.gov/
