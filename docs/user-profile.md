# Your Core account profile

Open **Profile** from the Core header or visit `/accounts/profile/` after
signing in. Your profile belongs to your Core account and is shared across its
organizations and authorized applications. You can edit your first name, last
name, and optional avatar image URL, then select **Save profile**.

Use a public HTTPS image URL without embedded credentials or a fragment. Core
stores only the URL; it does not download, upload, resize, or host your avatar.
Browsers and applications that show the avatar load it directly from the image
host. Core's profile preview sends no referrer. Leave the URL blank and save to
remove it. Avoid a URL containing private access tokens or other secrets.

The sign-in email and stable account UUID are displayed under **Account
identity**. Email is read-only here because Core does not yet offer a verified
self-service email-change flow. Ask a Core operator for account identity
corrections. Editing your profile does not change your password, memberships,
roles, developer access, or platform administrator privileges.

**Approved applications** lists your remembered application approvals and their
scopes. Select **Revoke approval** to withdraw an application's remembered
approval and revoke its current Core tokens. Core will ask you to approve that
application on a later sign-in. This does not delete information the application
has already received or automatically end an application's independent session.

## Application integration

The OIDC `profile` scope includes `name`, `given_name`, `family_name`, and
`picture` when an avatar URL is set. The separate `email` scope includes the
account email. Core does not assert `email_verified`. Applications should use
the issuer plus the stable `sub` UUID as the account key; names, email, and
avatar URLs are editable presentation metadata, not authorization grants.

An existing ID token is a signed snapshot. Applications can read current
profile values from `/oauth/userinfo/` using a valid access token with the
appropriate granted scopes, or obtain them on a later sign-in/token issuance.
Applications remain responsible for validating tokens and honoring current
service access as described in [OIDC integration](integration.md).

Core account metadata provides a central account reference for future
integrations. It does not represent a verified legal or billing identity, and
this profile feature does not collect payment details or process payments.

## Upgrade compatibility

Apply identity migrations through `0011_applicationconsent` before serving the
new Core version. Migration `0010_user_avatar_url` keeps an empty-string
database default for the optional avatar field, so an older Core process can
still create accounts while the upgrade is in progress even though its user
INSERT omits that column. Existing accounts begin with a blank avatar. Restart
Core processes on the new version to make profile and approval controls
available.

Apply platform migration `0040_user_profile_documentation` to register this
guide for the installed Core package version and inherit it into existing
enabled Core installations. Existing organization edits and pinned source
revisions are retained; rerunning documentation synchronization does not
replace those choices.
