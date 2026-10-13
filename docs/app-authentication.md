# App-branded authentication and invited accounts

Core remains the identity provider. Applications use authorization code + PKCE and receive the same stable Core user UUID; they do not keep a second password or synchronize duplicate accounts. Core grants application permissions independently for each organization/service/environment.

An active public browser registration with an exact registered callback can use its service's branding on login, consent, and invitation acceptance. The URL cannot supply an arbitrary app name, theme, or callback. Organization administration retains the Core interface.

## Administrator setup

1. Deploy the backend containing `identity.0009_service_auth_name_service_auth_tagline_and_more`, running migrations before serving the new image. See the [exact existing-AWS upgrade instructions](https://github.com/NorthCountryEngineer/Axolotl_News/blob/main/docs/APP_AUTH_DEPLOYMENT.md).
2. Open **Users and permissions → Services → your service → Service settings**.
3. Optionally set **Auth name**, **Auth tagline**, and **Auth theme**. Empty name uses the service name. Automatic selects Axolotl News for service slug `axolotl-news`, otherwise Core. These fields change presentation only.
4. Use **Invite a user**, enter their email, and choose **Member** for an ordinary application user. Core displays a one-use invitation link; it does not send email. Share the link privately.
5. The invitee opens the application and chooses **Create account with invitation**, then pastes the link. A new email creates one Core account; an existing email requires signing into that same account before joining. Invitation acceptance preserves existing passwords, UUIDs, and memberships.
6. Assign the intended role and environment access tag after acceptance. An ordinary invitation does not grant editing, publishing, other app access, or administrative rights. An administrator invitation still carries its explicitly selected administrator membership; choose Member for children/readers.

If an app's environment boundary is not yet granted, acceptance returns to its registered origin with `/?account=created`. The app explains that access needs approval. After the administrator grants access, the user starts a fresh sign-in. If the boundary is already allowed, acceptance resumes authorization directly; protected app APIs still enforce service permissions.

## Browser integration

Generate a normal authorization request containing the browser client ID, exact callback, `response_type=code`, `scope=openid authorization`, unpredictable state, and S256 PKCE challenge. Sign-in uses `/oauth/authorize/` as before. Invitation registration uses `/accounts/app-signup/?next=` with that **relative** authorization URL URL-encoded as its value. Keep the verifier and state in the initiating app tab and validate both on return.

Core validates the client, callback, organization, invitation expiry/use, and continuation before creating an account or returning to the app. CSRF checks and authorization consent remain enabled. Form pages retain `Referrer-Policy: same-origin`: external sites receive no referrer, and native form submissions retain the Origin needed for CSRF protection. They are not cached.

Open registration, email delivery, and automatic permission grants are not enabled. To add another app, register its own service/browser client/callback and assign access explicitly; the person continues to use the same Core account.

## Verification

```sh
.venv/bin/python manage.py --local test identity.tests platform_services.tests.test_external_apps --noinput
.venv/bin/python manage.py --local makemigrations --check --dry-run
```

Tests exercise invitation → central account → consent → PKCE token exchange → stable user UUID, existing-account reuse, CSRF, invitation replay/expiry, cross-organization denial, callback validation, and absence of implicit role/access-tag grants. Local tests do not establish that the new image has been deployed.
