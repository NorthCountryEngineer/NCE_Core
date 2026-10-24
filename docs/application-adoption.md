# Move an existing external application between Core organizations

`adopt_external_application` is an operator database procedure. It changes the
ownership of an existing external application without provisioning resources,
changing DNS, moving a shared hosting platform, or issuing provider requests.
It does not merge organizations. Normal application forms retain their
immutable placement rules.

Prepare an active receiving organization, its first explicitly chosen active
administrator, an embedded `core-platform` installation, and an active environment
with the same environment name as the application's existing environment. Use
the existing bootstrap, `install_core`, and `create_environment` procedures;
creating these database records creates no cloud capacity. Do not copy the
source organization's user roster or administrator roles wholesale.

## Review the exact operation

Run the command in the intended Core database, using existing immutable UUIDs:

```sh
python manage.py adopt_external_application \
  --application APPLICATION_UUID \
  --target-organization RECEIVING_ORGANIZATION_UUID \
  --target-environment RECEIVING_ENVIRONMENT_UUID \
  --actor OPERATOR_USER_UUID \
  --member-user EXPLICIT_OWNER_USER_UUID
```

With no apply argument the command is read-only. Its deterministic JSON plan
contains object IDs and state fingerprints, selected member UUIDs, policy tag
slugs, token revocation counts, and any blockers. It does not decrypt or print
OAuth secrets, encrypted bundles, private manifests, or configuration contents.
Treat the plan as private operational metadata and retain it outside Git.

The actor must remain an active superuser or an active administrator of both
organizations. The receiving organization must already have an active human
administrator. The target Core baseline is permitted; conflicting application
slugs, service slugs, package installations, or migration tag namespaces are not.
All releases must be terminal. A draft is unfinished and must be deliberately
cancelled or completed before cutover.

The command discovers application accounts, application pins and source roles
that contain the moved service's permissions. It maps those users by the same
global user UUID. `--member-user` explicitly includes an additional current
source member, such as the existing owner whose access came from being a source
administrator. New target memberships are ordinary members; existing target
administrator assignments are retained. The operation does not automatically
turn source administrators into target administrators. An active source
administrator who has neither an existing target administrator assignment nor
a valid explicit application account or moved-service role is a blocker; review
that user's intended target access before applying the operation.

Core role permissions for the moved service are copied into application-scoped
target roles and removed from the source roles. Unrelated source role permissions
remain. Published `ApplicationAccountAccess.role_ids` are manifest role identifiers,
not Core role database IDs, and remain unchanged. Pins and application accounts
are rebound to the corresponding target membership. Existing disabled memberships
are not silently activated. Effective tag grants from a selected user's direct
membership and all source roles are retained only where required by the moved
graph; unrelated role permissions and unrelated tag grants are not copied.
Supporting role and grant state is included in the reviewed fingerprint.
Policy restrictions previously inherited from the
source organization and environment are retained on the moved graph. Custom
tags receive new `adopt-<application UUID without hyphens>-<source tag ID>` slugs,
with their human labels and policy settings preserved. Existing target tags are
never reused for those custom tags, even when their names and settings match:
that could grant unrelated target resources. A collision in the migration's new
namespace is a blocker. Only the original environment boundary is remapped to
the target environment's native boundary tag, with identical reviewed policy
settings. The plan lists each source-to-target tag mapping.
Required tags bound to any other environment are blockers; they cannot be
converted into ordinary custom tags and lose their environment restrictions.

## Apply once, then verify the application

Quiesce the application and any background processing, review every blocker and
take the existing database/storage recovery checkpoints. Add the exact reviewed
fingerprint to the same arguments:

```sh
python manage.py adopt_external_application \
  --application APPLICATION_UUID \
  --target-organization RECEIVING_ORGANIZATION_UUID \
  --target-environment RECEIVING_ENVIRONMENT_UUID \
  --actor OPERATOR_USER_UUID \
  --member-user EXPLICIT_OWNER_USER_UUID \
  --apply-plan-hash EXACT_REVIEWED_PLAN_HASH
```

Apply locks the application, source/target organization, related rows and grant
edges, rechecks the complete fingerprint and actor authority, then performs the
change in one database transaction. An outdated plan or any reported blocker
prevents changes. A failure rolls back the graph, grant mappings and new audit
events together. Replaying a completed adoption is refused, rather than applying
the move twice.

Application, installation, service, deployment, OAuth client IDs and encrypted
client credentials stay the same. Existing external deployment parents are
remapped only from the source's embedded `core-platform` to the target's embedded
`core-platform`. The source hosting platform, its environment, vault and
configuration remain provider-owned. An exact per-application
`CloudHostingAssignment` records the provider and receiving organization boundary.
An exclusive private package follows its installation; a shared catalog package
retains catalog ownership. A private package shared by other installations is a
blocker. Completed release snapshots, documentation revisions and existing audit
records retain their original provenance. Both organizations receive a new
append-only adoption audit event bound to the plan fingerprint.

Only the moved clients' access tokens, refresh tokens, authorization codes and
ID tokens are revoked at cutover. Other applications' tokens and Core browser
sessions remain. Fresh application sign-in is required under the receiving
organization. The public origin, physical cloud resource names, application UUID
and provider prefix remain stable. There are no DNS or provider-side operations.

Unknown integration, agent/task approval, chat, development, federation or other
consumer dependencies are reported as blockers with their model and record IDs.
They are not silently deleted, transferred with broader permissions or left
cross-organization. Review and extend the migration deliberately if any exist.
An application's own agents and integration settings stored in its external
database are distinct from these Core-managed approvals.

**This operation does not migrate an application's database or files.** Coordinate
its application-side tenant mapping, private ownership, object/reference namespaces,
encrypted vaults, sessions and membership bindings separately. Retain byte/count
checks and rollback evidence before reopening access. Verify a fresh login,
permission revocation, application data/files/history, integration bindings and
future release preparation. Do not claim a complete tenant move from a Core
ownership change alone. See [cloud deployments](cloud-deployments.md) and
[external application registration](external-applications.md).

`platform_services.tests.test_application_adoption` exercises the procedure on
disposable local databases with HTTP and provider mutation transports mocked to
fail. It checks exact-state drift, transactional rollback, retained identities,
private credentials/history, scoped membership/role mapping, current permission
resolution, token revocation scope, dependency blockers and command defaults.
