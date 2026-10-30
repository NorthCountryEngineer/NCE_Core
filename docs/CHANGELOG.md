# Changelog

## 2026-10-30 — Exact-plan release cancellation and frontend publisher repair

Failed or revoked releases retain the approved plan hash when requesting runner
cancellation, matching the fixed Lightsail protocol's original request contract.
An operator-only plan/apply helper repairs the existing frontend publisher with
only Proposal Studio's fixed production manual-deployment resource and the two
existing create/start actions. It preserves all prior policy statements, isolates
the explicitly selected profile from inherited AWS credentials/endpoints, checks
the AWS account and exact before hash, records intent before the single update,
verifies the after hash, and refuses automatic retries of an unknown outcome.
The release includes the already-deployed Lightsail adapter source in Git;
unrelated older checkout changes are excluded. See the [operating guide](lightsail-deployment.md).

## 2026-10-26 — PostgreSQL-safe authentication evidence locking

Registration maintenance locks only the selected authentication-evidence row while
reading optional federated identity/provider metadata. Password-only evidence
therefore works on PostgreSQL without attempting to lock the nullable side of an
outer join. Existing actor, freshness, revocation and policy checks remain intact;
focused registration and exact operator checks run against disposable PostgreSQL.

## 2026-10-26 — Existing login evidence for registration maintenance

The shared-runtime registration command accepts an explicit existing actor-bound
login-evidence UUID and optional current policy purpose. Organization authentication
policies, authentication age, MFA, federation, live administration and resource
tags are checked for the host, provider and every target. Evidence is not created
or inferred; revoked, foreign, stale and malformed proofs fail without changing
the allowlist. The selected evidence is linked in the existing audit trail. No
schema, hosting, IAM, secret-store or subscription change is required.

## 2026-10-26 — Durable shared-runtime registration credentials

Added an operator-only, audited deployment allowlist in the existing cloud
application configuration. Exact reviewed releases pin the primary/additional
public identity bindings and rebuild the existing Standard SSM bundle from
current Core records, so later releases preserve shared-runtime organizations
without reading foreign secret-store data. Unavailable/mismatched registrations,
stale rotated credentials, malformed bundles and the serialized 4 KiB limit
fail closed before writing. Normal external-resource secret rotation updates its
paired encrypted deployment copy transactionally. The command requires current
hosting/provider and target administrator authority and cannot race an active
release. No table, IAM permission, SSM tier, service, hosting or subscription
change is introduced. See [registration maintenance](external-applications.md#keep-shared-runtime-registrations-across-releases).

## 2026-10-26 — Independent organization context discovery

Added metadata-only external-application context discovery for a bounded,
operator-configured list of organization deployments sharing the exact public
origin and callback. The source user's paired resource client and live
`workspace-use` access are required; each target independently checks current
membership, permission, deployment availability, authentication and tag policy.
Discovery creates no memberships, roles, installations, credentials or hosting
resources and returns no secrets. Applications must complete separate target
PKCE authorization before switching authority. See [external applications](external-applications.md#multiple-organizations-at-one-application-origin).

## 2026-10-26 — Plain authentication and operational copy

Removed promotional taglines and decorative authentication headings from Core sign-in and invitation pages, including the default “One account. Your next adventure.” and previously configured service taglines. Service settings retain the app name and theme controls while hiding the unused tagline field; existing stored values remain intact without a migration. Cloud application and chat empty states use direct status text. Application branding, access explanations, invitation handling, OAuth validation, and consent behavior are unchanged.

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


## 2026-10-27 — Lightsail hosting adapter preparation

Added the portable Core launcher and an operator-bound Lightsail deployment provider for existing container applications. Human approval, exact CI/source/binding checks, existing identities and credential bundles remain. A fixed SSH runner accepts only staged immutable artifacts, preserves quiesced database backups, and stops safely on uncertain application writes. Unstaged commits are refused. Existing unbound AWS recipes remain unchanged; no cloud mutation or live acceptance is claimed by local tests. See [operating guide](lightsail-deployment.md).

## 2026-10-28 — Automatic bounded Lightsail source builds

Reviewed administrator releases retain exact-commit CI admission and now build
source automatically in isolated on-demand CodeBuild. The fixed host runner
verifies project/source/build/artifact provenance and AMD64 image digests before
maintenance, migration and existing Amplify publication. Root-only minute reserves
and durable recovery prevent duplicate or unbounded builds. Normal releases need
no manual artifact staging; no ECS/RDS/NAT provisioning fallback is available for
bound applications. Source patches, local checks and live acceptance remain
separately recorded in the Lightsail operating guide.
