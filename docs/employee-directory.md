# Employees and reporting relationships

Organization administrators manage employees in **Users and permissions → People**.
The directory belongs to NCE Core so connected applications can use the same stable
organization and person identities when a separately authorized integration is added.
This release provides Core administration; it does not change a connected application's
record-sharing policy or automatically expose personnel data to applications.

## Define people and managers

Create a person with a work email and organization-specific name, title, department,
and optional employee code. A person may exist before they have a login. Mark a person
as a manager to make them eligible for reporting assignments. A manager is also an
employee and may report to another manager; a manager does not need reports to exist.

Select managers explicitly. An employee can have several assigned managers and at
most one active primary manager. Core rejects self-management, reporting cycles,
cross-organization links, and inactive assignment endpoints. Removed assignments
remain inactive records rather than disappearing. Revision checks reject stale forms
instead of overwriting another administrator's changes.

Search and status filters apply within the selected organization. Employee names,
emails, reporting links, and external references are personnel information; the
directory is administrator-only and observes existing resource tags, authentication
assurance, and purpose restrictions. It is not a public organization chart.
Each person links to their authorized managers and direct reports so administrators
can follow multiple levels of the reporting structure without opening unrelated
employee records. Bounded manager searches preserve existing assignments and refuse
an incomplete replacement.

## Create login access

A directory record and a Core account are separate. An administrator can explicitly
link an existing membership in this organization or create a single-use account
invitation for a pending person. Core displays a setup link once; share it securely.
No email delivery service is started. The person chooses their own password; an
existing Core account must sign in as the invited account before joining.

Only the invitation stored on that directory record can link its accepted membership.
An email match alone never joins accounts or grants organization access. Expired,
revoked, consumed, or mismatched invitations cannot attach a person, and acceptance
does not reactivate disabled memberships or copy roles. Changing a pending work
email invalidates the old invitation before another can be issued.

The directory's employment status and login access are shown separately. Setting a
directory record inactive does not disable a global account used in other
organizations. To remove this organization's access, use its existing membership
editor and disable the membership. Existing live Core checks enforce that change.
The final active organization administrator remains protected.

## Reporting structure and permissions

Reporting relationships do not change `Membership.kind`, custom service roles,
permission tags, billing authority, or administrator status. Managers receive no
implicit access to an employee's Proposal Studio artifacts, chat, private files, or
other application data. A future manager-facing application view must require both
its explicit service permission and a current authorized reporting relationship.
Relationships are never embedded in an OAuth token as a durable access grant.

## Future HR integrations

Core stores provider-neutral external references separately from authentication
identities. A reference includes the organization, a source namespace identifying
the provider connection, external object type, opaque external ID, and optional
source revision. One source object cannot silently move to another person.
Different organizations and source namespaces may use the same external ID.

The existing [integration catalog](application-integrations.md) remains the future
authority for connections, environment placement, encrypted credentials, revisions,
and explicit consumer grants. External reference records contain no credentials and
cannot authenticate a person or grant permissions. No provider connector, sync job,
webhook registration, polling schedule, subscription, or remote request is enabled
by this directory feature.

A future adapter should normalize provider data into bounded proposed changes,
deduplicate by connection/object/version or content hash, preserve administrator
overrides, and surface conflicts. It must keep candidate, hired employee, login
account, and access grant as separate states. Partial retrieval or a missing page
must not delete employees or revoke access. A reviewed offboarding policy can
explicitly disable the organization's membership without disabling the person's
other organization memberships.

For Greenhouse, Recruiting users are ATS accounts and candidates/applications are
recruiting records; they are not the employee roster. Greenhouse Onboarding has
employee records with direct-manager and HR-manager fields. Keep those relationship
types distinct when the adapter is implemented. Recruiting integration should use
the current Harvest v3 contract, with its OAuth scopes, bounded cursor pagination,
rate limits, and validated webhook signatures. Recheck these contracts before
implementation: [Harvest v3 authentication](https://harvestdocs.greenhouse.io/docs/authentication),
[Onboarding employee API](https://docs.greenhouse.io/gho.html#employee),
[Recruiting webhooks](https://docs.greenhouse.io/webhooks.html).

## Local verification

Use fresh synthetic people, invitations, organizations, and isolated databases.
Directory tests cover tenant and tag isolation, administrator/CSRF checks,
primary/multiple managers, self/cycle rejection, stale revisions, explicit invitation
linking, inactive-account rejection, and external-reference identity boundaries.
PostgreSQL checks exercise concurrent graph changes under the organization lock.
No HR account, provider key, live cloud resource, email delivery, or model call is
needed. Local verification does not publish or deploy the feature.

## Upgrade

Apply identity migrations through `0013_employee_directory` and platform migrations
through `0041_employee_directory_documentation` before serving this release. The
identity migration follows `0012_application_accounts`; the documentation migration
follows the already-published `0040_user_profile_documentation`. Directory tables
start empty. The upgrade does not infer employees from existing accounts, change
passwords or memberships, assign managers, or enable an external integration.
The guide migration preserves organization edits and pinned documentation sources.
