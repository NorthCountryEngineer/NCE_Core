# Changelog

## 2026-10-24 — External application ownership adoption

Added an operator-only, exact-state plan/apply operation for moving an external application to another organization while retaining its service, deployment, OAuth clients and existing infrastructure identifiers. A per-application hosting assignment preserves the provider's separate platform authority; receiving administrators can view their application's history without gaining provider credentials, settings or unrelated applications. Unclassified dependencies and active releases prevent a partial move. Application tokens are revoked and both organizations retain migration audit evidence. See [cloud deployments](cloud-deployments.md) and [security](security.md#external-application-ownership-adoption).

The operation does not provision infrastructure, change DNS, invoke models or send integration messages. Local disposable database and mocked cloud checks precede any publication or production cutover.

This change retains the corrected Core templates without purpose inputs, Game Studio deployment recipe, and its separately approved [owner-dispatch CI policy](proposals/game-studio-owner-dispatch-ci.md). Adoption does not modify that policy or its exact source/workflow checks. The separately approved [one-release Core broker](proposals/game-studio-one-release-broker.md), its fixed Game Studio checkout and its bounded expiry remain unchanged.

## 2026-10-22 — Transient empty CI indexes

The exact-commit gate can reread a genuinely empty repository index up to three
times with fresh public cache keys. Final consistency checks also use a fresh
key. Nonempty, failed, pending, malformed and inaccessible results retain their
immediate refusal behavior; successful parent and exact-attempt job checks are
still required. Application and runner admission share a 40-second, 20-read
allowance. Offline tests cover transient and persistent emptiness, final-check
races, strict refusal, transport budgets and the shared two-source allowance.
No new CI runs, cloud resources, schema changes or model calls are introduced.

## 2026-10-22 — Exact-commit CI lookup compatibility

When GitHub's workflow-specific run index is empty, Core can verify the same
exact push commit through the complete bounded repository index. All run
provenance, trusted workflow, latest attempt, completed-success parent, required
job and deployment-stage rechecks remain enforced. Unrelated workflows never
authorize release. See [CI/CD admission](cicd.md#exact-commit-deployment-admission).
Offline regressions cover empty versus malformed indexes, unrelated workflows,
duplicate IDs, pending/failed attempts and changes during verification. No
publication, new CI execution, cloud resources or provider calls are performed
by the lookup itself.

## 2026-10-19 — Organization employee directory

Added local administrator-managed employee profiles and manager assignments in Core,
separate from application permissions. People can be defined before account access,
linked to an existing organization membership, or given a securely bound invitation.
Reporting changes reject cross-organization links, self-management, cycles, duplicate
primary managers, and stale revisions. Provider-neutral external references leave
room for HR integrations without enabling a connector, polling, email service, or
subscription. See [Employee directory](employee-directory.md). The combined release retains the already-published account profile, remembered
application approvals, live revocation and Academy PII runner protections.

The directory and delegated application accounts use consecutive additive identity
migrations (`0012_application_accounts`, `0013_employee_directory`). Directory
documentation is `0041_employee_directory_documentation`, after the published profile
guide. Invitation acceptance runs both explicit binding checks in one transaction;
an email match does not join a directory person to an application account.

Validation includes disposable PostgreSQL, actual loopback HTTP invitation/account
setup, concurrent reporting changes, immediate revocation, tenant/tag/administrator
isolation, and cross-feature permission boundaries. The exact combined release commit
must pass GitHub Actions before deployment. No HR provider, email service, cloud
resource, or paid model call is needed for these local checks.

## Application accounts

- Added human-delegated, app-scoped account invitations, list and revocation.
- Kept passwords and stable user identities in Core; separated application roles from organization administrator privileges and environment-wide access.
- Added an additive identity-package upgrade command and application-account migration. No infrastructure is deployed by account configuration.

## Combined-release safety fixes

- Ordinary Core invitation revocation now takes the organization lock before the
  invitation and any linked people records, matching acceptance and directory edits.
- Delegated application administrators can list and revoke their own application's
  eligible invitations without receiving organization or environment grants. Exact
  deployment filters and explicit/inherited identity and personnel tags remain enforced.
