# Application account administration

An external application can delegate account invitations to an administrator while keeping Core as the only identity store. The application backend authenticates with its existing confidential resource client and forwards the current human access token. Core rechecks the exact registered browser/resource client pair, organization, deployment, environment, human membership and permission on every call. The actor needs Core's reserved `admin` permission or this service's explicit `accounts-manage` permission. Publishing content alone does not allow account administration.

The reviewed installation manifest publishes `account_roles` containing an ID, name, description and this application's declared permission codenames. It cannot grant Core's reserved `admin`, another service's permissions, organization administrator membership, organization roles, or organization-wide environment access. Axolotl News publishes Editor, Publisher and Administrator; the latter includes `accounts-manage` and the separate `agents-manage` permission for assistant configuration. Publisher access cannot enable paid execution.

The administrator supplies an email, optional name and one published role. Core creates a one-use invitation, retains only its token digest and returns its private link once. The application does not send email or receive passwords. Invitations expire in 48 hours and can be revoked before acceptance. Listing never returns a previously issued raw link. Requests have bounded payload/list sizes, private noncached responses and append-only Core audit events.

The invitee accepts the invitation in Core. A new email creates one Core account; an existing email requires sign-in as that existing account, preserving its UUID and password. Account creation and invitation consumption are one transaction. The assigned permissions are stored separately for the exact application deployment. An existing application account (including inactive access) or legacy application role assignment cannot be reinvited. Invitations never reactivate disabled access or silently replace an existing role. No other application grants or organization membership privileges change. The new application account can satisfy only its own OAuth/service/permission environment boundary, so its environment access cannot open another app, data resource or administrative API. Core's existing MFA, purpose, other permission tags, disabled environment/installation and current identity controls still apply.

Roster and invitation reads remain limited to the authenticated application's exact
registered deployment. Those organization-owned identity records do not inherit an
application placement solely because the request came from an app; their own,
organization and linked personnel tags still apply. Explicit tag grants, MFA and
purpose restrictions remain required. This grants no general environment access,
Core personnel-directory access, or authority over another app's invitations.

The machine endpoint is `POST /api/v1/application-accounts/` with HTTP Basic resource-client credentials and form fields `token`, `operation` and JSON `payload`. Operations are `list`, `invite` and `revoke`. The browser calls its application API rather than receiving the resource secret. Browser/session authentication and CSRF exemptions cannot authorize this endpoint; both the client secret and a valid appropriately authorized human token are required.

## Existing installation upgrade

Release the Core image containing `identity.0012_application_accounts` and run migrations before serving it. The migration adds only two tables for app account grants and invitations; it does not change existing users, memberships, passwords, grants or invitations. The migration follows deployed `0011_applicationconsent`. The combined directory release then applies `0013_employee_directory`, and documentation migration `0041_employee_directory_documentation` follows the existing profile guide migration `0040_user_profile_documentation`. Application grants and directory records remain independent: neither invitation flow links another record by email or implicitly assigns the other feature’s permissions.

Upgrade the existing package using a reviewed file and exact service ID:

```sh
python manage.py upgrade_external_application \
  --organization family \
  --service-id YOUR_EXISTING_SERVICE_UUID \
  --package examples/axolotl-news-package.json \
  --expected-version 1.0.0 \
  --actor-email YOUR_EXISTING_CORE_ADMIN_EMAIL
```

This additive identity-only command preserves existing capability grants, clients, SSM credentials and external deployments. It creates no cloud resource or person and emits only nonsecret confirmation. Retrying the exact already-applied package is a no-op. Once Core and the package are ready, deploy the application's account API and frontend. Administrators can then create and revoke invitations from the app. Account management requires live acceptance testing with authorized synthetic identities separately from source tests; do not create real people as a deployment check.
