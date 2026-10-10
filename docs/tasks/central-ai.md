# Central AI service implementation

Owner of every task: Codex. No external agents, model calls, or paid infrastructure are required.

| Task | Deliverable | State |
| --- | --- | --- |
| AI-01 | Map identity, application RBAC, environments, data fabric, marketplace, and shared usage contracts | Complete |
| AI-02 | Register Core AI as a managed, $0 marketplace service | Complete |
| AI-03 | Define administrator-owned, permission-tagged query sources with field and row policies | Complete |
| AI-04 | Implement bounded, paginated queries across application datasets and approved deployment federation | Complete |
| AI-05 | Preserve delegated user authority, explicit human federation grants, and live revocation checks | Complete |
| AI-06 | Add evidence-only conversational retrieval, citations, and an inactive provider contract | Complete |
| AI-07 | Add application SDK discovery and safe query tools | Complete |
| AI-08 | Verify RBAC, tags, row policies, quotas, federation, and real browser workflows | Complete |
| AI-09 | Document administrator/user setup, update cloud setup, and publish verified code | Complete |

Out of scope for this foundation: paid model calls, API-key management, Chat changes, unrestricted HTTP/SQL connectors, semantic indexing, and automatic natural-language query planning. Applications publish through the existing data pipeline, or expose datasets through bilaterally approved Core Data Fabric agreements. Records remain at their authority during federation. Retrieval never treats record text as instructions or authorization.

Validation: the full 498-test Django suite passed on disposable PostgreSQL (497 passed, one optional integration skipped), including Chromium source configuration, member retrieval/pagination, role revocation, and independent-deployment HTTP federation. The Core AI suite passed on local SQLite (34 passed, browser verification skipped there). An additional Unicode retrieval regression passed after correcting escaped-text matching. Thirty CI unit tests passed. Migration drift, system checks, and staged secret scanning passed. Disposable database containers and volumes were removed; existing live accounts and keys were preserved.

Use [the Core AI setup and integration guide](../central-ai.md) to configure application sources and test user access. Generated answers, model secrets, semantic indexing, and autonomous agent retrieval remain future tasks; no paid calls or external deployments were performed.
