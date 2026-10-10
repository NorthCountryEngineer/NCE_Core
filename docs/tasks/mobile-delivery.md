# Sequential iOS and Android delivery playlist

Goal: one shared Core Platform UI, distributed through both stores, with a
member signing in and launching an administrator-installed, $0 application.
See [the architecture review](../mobile-architecture.md) for the design and gaps.

Codex owns repository implementation. The human owns purchases and every action
performed as the owner outside GitHub/NorthCountryEngineer. Execute tasks in
numeric order, **one implementation task per session**, without concurrent tasks
or delegated agents. Finish and verify a task, update this ledger, then stop.
A later prompt starts the next task. Do not silently skip a human prerequisite.

Status after this session: MOBILE-01 complete; MOBILE-02 is next; MOBILE-03
through MOBILE-15 remain queued. No native package or store deployment exists yet.

| ID | Task and acceptance criteria | State |
| --- | --- | --- |
| MOBILE-01 | Review architecture; choose native hosts around the shared UI; implement safe-area ownership, readable phone controls and inset-aware header drawers; verify browser geometry and existing security behavior. | Complete |
| MOBILE-02 | Build the first-party native authentication transaction/session handoff and public mobile-client configuration. Preserve exact callback/issuer/tenant binding, S256 proof, single-use expiry, MFA/purpose evidence, CSRF and live revocation. Reject third-party token escalation, replay and cross-tenant redemption. Ship meaningful backend security tests and a native integration contract; no external IdP account actions. | Next |
| MOBILE-03 | Create the Swift/WKWebView project with reproducible non-secret configuration, approved HTTPS origin, system-browser sign-in, platform cookie installation, cancellation/logout and a bundled offline screen. Keep remote JavaScript unprivileged. Verify an unsigned simulator build on available macOS/Xcode; if unavailable, stop for the tooling handoff. | Queued |
| MOBILE-04 | Create the Kotlin/Android WebView project with pinned/verifiable Gradle tooling, equivalent configuration/auth/cookie handling, safe navigation and Android Back behavior. Release configuration forbids cleartext and debug origins. Verify a debug/emulator build; stop before accepting SDK licenses or configuring an external account as the owner. | Queued |
| MOBILE-05 | Implement shared member application launcher and native navigation policy. Admin installs/deploys a $0 app; member sees only live-authorized instances. Approved Core/app links preserve organization/environment context; ordinary external links use the system browser. Add verified HTTPS callback/deep-link association templates without modifying external DNS/hosting accounts. Test tampered links and untrusted origin redirects. | Queued |
| MOBILE-06 | Audit the complete shared UI: marketplace/admin pages, app launcher, organization switching, header pins, Chat groups, AI evidence/pagination and docs. Fix safe areas, virtual keyboard/composer reachability, horizontal tables, tap targets, text scaling, VoiceOver/TalkBack focus and portrait/landscape behavior. Verify both native hosts using identical web assets. | Queued |
| MOBILE-07 | Build Core push-device registration, permission-scoped notification routing and native APNs/FCM adapters with local fake transports. Recheck user/room/tag permissions before dispatch and opening a notification. Use generic lock-screen content; handle denied permission, logout, token rotation and revocation. Real provider setup/delivery waits for the human actions in MOBILE-13/14. | Queued |
| MOBILE-08 | Implement account deletion/request status and retention-aware organization handling, Chat report/block/moderation and support workflows. Prepare accurate privacy/data inventory, required consent and permission explanations. Keep paid model generation and billing inactive. Test abuse-report access boundaries and deletion without granting another tenant access or erasing required audit evidence indiscriminately. | Queued |
| MOBILE-09 | Extend portable CI with separate sequential server, iOS and Android verification/package stages. Document approved self-hosted macOS/Android runner configuration and adapters for existing CI providers. Pin dependencies/checksums; protect signing inputs and outputs; produce versioned unsigned artifacts first. Workflows remain manual and paid runner/publication gates remain closed. No runner dispatch or external CI account setup without the applicable handoff. | Queued |
| MOBILE-10 | Validate an owner-supplied, production HTTPS backend and review sandbox: issuer, TLS, static files, database/workers, secret mounts, policy isolation and sample app availability. Provide deployment artifacts first. Stop for human DNS, hosting, cloud/IdP account actions or costs; do not purchase capacity or reconfigure those services as the owner. Record verified readiness once the human returns. | Queued |
| MOBILE-11 | Run sequential simulator/device acceptance: administrator installs the $0 app, member signs in/launches it, unauthorized user is denied, roles/tags/environment revocation take effect, logout clears private state, app restart/expired session/offline recovery work. Check external-provider flow against approved local test fixtures. iOS device signing, if needed, is a human prerequisite. Record actual OS/build/device results; real push remains gated. | Queued |
| MOBILE-12 | Prepare reviewed app name, bundle/package IDs, icons, launch assets, screenshots from both hosts, support/privacy/deletion URLs, ratings, privacy manifests/data declarations, review steps and a synthetic least-privileged review account in the existing sandbox. Confirm current store/SDK/login/billing rules. Do not reserve identifiers or enter store-console metadata as the owner. | Queued |
| MOBILE-13 | Human completes/reuses Apple enrollment, App Store Connect app/identifier, signing assets, associated-domain hosting and APNs configuration. Codex builds/verifies the signed release locally only when secure inputs/tooling are available; human uploads to TestFlight and manages reviewers/testers. Verify iOS push and review feedback after the human reports results. Stop at every external account action or fee. | Queued — human handoff |
| MOBILE-14 | Human completes/reuses Google Play registration, package/app setup, upload-key/Play App Signing, App Links hosting and Firebase/FCM configuration. Codex builds/verifies the release AAB with secure local signing inputs; human uploads and runs required internal/closed testing. Verify Android push and feedback; meet the account-specific testing requirements shown in Play Console. Stop at every external account action or fee. | Queued — human handoff |
| MOBILE-15 | Recheck both release candidates against the server and current store declarations, resolve review findings sequentially, and provide final submission/release instructions. Human submits and controls production availability in each store. Record store listing/version URLs and verify the released member/app workflow when the human returns. Define rollback, signing/key rotation and future backend/native compatibility maintenance. | Queued — human handoff |

## MOBILE-01 completion record

The shared base now handles screen insets once and retains pinch zoom. Phone
inputs/selects/textareas use at least 16px text, primary form controls have 44px
minimum height, and long product names wrap. The header component handles
embedded-page insets separately and keeps drawers clear of side/bottom insets.
Chat-specific styles no longer override the shared phone field sizes.

Three real Chromium tests passed using actual templates/component code and
local synthetic APIs: login/AI at 320, 390, 768 and 1280px; header geometry with
simulated insets and 844/500px viewport heights; and Chat form/control sizing.
The combined focused run passed all 41 tests: those three browser tests plus
38 existing CSRF/header behavior and authorization tests on disposable SQLite.
Authentication, actual native insets/keyboard behavior, all admin tables and
device/store builds are later acceptance criteria, not completed claims.

Run the focused layout checks using an already-installed browser:

```sh
CORE_DEBUG=1 CORE_BROWSER_EXECUTABLE=/path/to/chromium \
  uv run --group browser python manage.py test identity.tests.test_mobile_ui --noinput
```

The cloud verification used `/usr/bin/chromium`. On a Mac, use an existing local
Chromium/Chrome executable path. No browser is downloaded by this test. Without
`CORE_BROWSER_EXECUTABLE`, these three optional browser tests are skipped, not
passed. Existing development keys stay in ignored `.local/`; do not run tests
against a customer database.

To continue, send: **“Start MOBILE-02 only. Keep the financial and external-account
handoff rules.”** Later tasks use the corresponding ID and the same stopping rule.

## Handoff procedures when a prerequisite is reached

These are future human actions. None is needed to inspect MOBILE-01, and Codex
does not perform them now. At an actual blocked step, provide the exact requested
configuration/artifact plus these instructions; stop before the account action.
Report only non-secret completion/results in chat. Keep signing keys, provider
credentials, recovery codes and account tokens in the selected secret manager or
ignored protected local files, never Git, screenshots or chat.

### macOS/iOS tooling or device signing

1. On the Mac, check `xcodebuild -version`. If Xcode is missing, the human installs
   it using their chosen Apple download/App Store account and reviews any terms.
2. The human opens Xcode and completes required first-run components/license
   acceptance. A simulator build does not require paid store enrollment.
3. If physical-device signing requires an Apple account, the human configures
   their own team in Xcode; Codex does not log in or accept account terms for them.
4. Reply **“MOBILE-03 tooling ready”** (or the blocked task ID), with Xcode/macOS
   versions or the non-secret build error. Codex resumes the remaining task.

### Android tooling

1. The human installs [Android Studio](https://developer.android.com/studio) if
   absent, chooses the SDK/emulator versions specified by MOBILE-04 and reviews
   the relevant license prompts themselves.
2. Confirm an emulator or test device is available. No Play registration is
   required for a local debug build.
3. Reply **“MOBILE-04 tooling ready”**, with SDK/JDK versions or a non-secret error.

### HTTPS hosting, DNS, identity or push accounts

1. Review the concrete deployment/configuration produced for the blocked task.
   Reuse existing infrastructure where suitable; check provider charges before
   buying capacity, domains or hosted runners. Approval of a configuration is
   not permission for Codex to spend money.
2. The human applies required cloud/DNS/IdP/Firebase/Apple account changes and
   places credentials in the specified secure destination.
3. Share only public origins/identifiers, the existing namespace, certificate
   status and whether secure inputs are available. Do not send secret values.
4. Reply **“MOBILE-XX prerequisite ready; resume verification”**, or redirect to
   a different existing provider/deployment. Codex verifies readiness and resumes.

### Apple store distribution

1. The human visits [Apple Developer](https://developer.apple.com/programs/) and
   reuses an existing account or reviews/enrolls in the appropriate program.
   The human verifies current fees and explicitly decides whether to pay them.
2. In their account, the human reserves the reviewed bundle identifier, configures
   capabilities/signing and securely supplies required local signing inputs.
3. In [App Store Connect](https://appstoreconnect.apple.com/), the human creates
   the app, enters the prepared metadata/privacy declarations and uploads the
   verified archive with their own tools/session. They manage TestFlight testers
   and any review correspondence themselves.
4. Reply **“MOBILE-13 TestFlight build ready”**, with version/build number and
   non-secret validation/review results. After MOBILE-15 verification, the human
   submits for review and chooses release timing.

### Google store distribution

1. The human visits [Google Play Console](https://play.google.com/console/), reuses
   an existing developer account or reviews registration, fees and identity
   verification. The human decides and performs any payment themselves.
2. The human creates the reviewed package/app, manages Play App Signing/upload
   keys securely, and configures required Firebase/FCM/App Links account settings.
3. The human uploads the verified AAB, enters prepared Data Safety/privacy/content
   information, and performs the internal/closed testing required for their
   account. They recruit/invite testers themselves; no automatic outreach occurs.
4. Reply **“MOBILE-14 Play test build ready”**, with version code and non-secret
   testing/validation results. After MOBILE-15 verification, the human submits
   and controls production rollout in Play Console.

Human actions do not bypass remaining verification. Apple/Google may require
changes, additional review or account-specific prerequisites; Codex records those
and handles the associated repository work in the next sequential task.
