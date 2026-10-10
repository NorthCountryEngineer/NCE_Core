# Accessibility review and verification

Core's shared web UI targets WCAG 2.2 Level AA. This review improves the existing
Django pages, organization header, Chat workspace, documentation reader, and
App Factory's generated web UI. It does not certify conformance or complete the
native iOS/Android accessibility task in the mobile playlist.

## Changes from the review

- Every shared page has a visible-on-focus skip link, a focusable main landmark,
  a descriptive page title, and consistent keyboard focus indicators. Login no
  longer bypasses the skip link by automatically focusing the username field.
- Invalid forms focus an error summary with links to the affected controls.
  Header editor help text and added link rows retain correct label/help IDs;
  newly added rows receive focus. The appearance preview is inert so its
  nonfunctional controls cannot receive keyboard focus.
- Administrative tables retain native table semantics, column headers, and
  captions inside named scrolling regions. Overflowing tables, documentation,
  and diagnostic/code blocks can be scrolled with a keyboard without adding
  tab stops to content that fits.
- Chat's muted text and control borders have stronger contrast. Message
  actions have larger targets. Header hover/shortcut styles preserve the
  administrator's validated text/background contrast, including near the
  minimum allowed contrast. Focus indicators work with forced colors, and
  scrolling/panel effects respect reduced motion.
- Header tools and notification panels move focus to available controls on
  opening and restore it on closing. Polling preserves focus in changing lists
  and application searches. Revoked permissions, removed conversations, and
  temporary outages move focus out of removed or hidden private controls.
  Changed notification counts and new messages have live announcements.
- Completed Core AI queries focus their results. Generated applications retain
  focus after record creation, label their forms and output regions, report
  loading state, and inherit the shared navigation, mobile, and focus styles.
- Production 403, CSRF rejection, 404, and 500 responses have accessible landmarks, titles,
  navigation, and generic error messages. The 500 template needs no database or
  request context to render.

The organization tools panel is nonmodal: users can leave it with Tab. Escape
closes it from the panel or header; F6 moves between it and its header button. Moving
focus into the application dismisses the panel without stealing that focus.
Chat notifications similarly support Escape and return focus to Notifications.

These changes preserve server-side authorization, permission tags, CSRF checks,
and private-data clearing. Appearance and focus behavior do not grant access.
Existing generated application releases need a reviewed rebuild/release to
inherit the updated template; updating Core alone does not rewrite their code.

## Automated checks

The optional browser tests use real templates/components and loopback-only
synthetic API fixtures. They create no external account and use no customer
data, AI provider, desktop push permission, native build, or paid service.

Pinned axe-core 4.10.3 checks WCAG 2.0/2.1/2.2 A/AA tags and best practices without
disabling individual rules. Coverage includes 23 representative screens at
390px and 1280px, eight open-panel states across those widths, and 24 themed
hover states: all four presets plus two near-minimum custom color schemes.
Screens cover login, organizations/members, administration, marketplace,
environments, modernization, onboarding, documentation and code output,
development objects, Data Fabric, AI search/results/admin, Chat, invalid forms,
the header editor, generated applications, and production error pages.

Ten keyboard/state tests cover skip navigation, error links, panel navigation,
dynamic list/search focus, access revocation, temporary outages, horizontal
scrolling, reduced motion/forced colors, editor help references, query results,
and record creation. Existing mobile layout checks and production error-page
tests run alongside them. Generated-service runtime tests verify the new CSS
route's MIME type and retain the arbitrary-file access restrictions.

From the repository, with Node/npm and an already-installed Chrome/Chromium:

```sh
uv sync --frozen --group browser --group dev
npm ci --prefix ci/accessibility --ignore-scripts --no-audit --no-fund
CORE_DEBUG=1 CORE_BROWSER_EXECUTABLE=/usr/bin/chromium \
  uv run --group browser python manage.py test \
  identity.tests.test_accessibility identity.tests.test_mobile_ui \
  identity.tests.test_presentation --noinput
```

On a Mac with Chrome already installed, replace `/usr/bin/chromium` with
`"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"`. The tests do not
download a browser. Without `CORE_BROWSER_EXECUTABLE`, browser checks are skipped;
without the pinned axe package, the three rules tests are skipped. Skipped
checks are not a passing accessibility audit. `CORE_AXE_PATH` can point to an
existing copy of the same pinned axe script. Use the local configuration from
[the testing guide](local-testing.md).

Verification on 2026-10-10 used local SQLite and installed Chromium: the full
regression run completed 518 tests with five optional checks skipped
(PostgreSQL-only concurrency/browser checks and the local Kubernetes integration).
The final focused run passed 26 browser, presentation, CSRF, and source-generation
checks without skips; the emitted service passed its 18 runtime tests. Two final
header/theme checks also passed after refining Escape behavior and checking
keyboard focus contrast. Axe reported no violations in the 78 reviewed states.
Frozen npm installation, Django system/migration checks, JavaScript syntax
checks, and the staged secret scan passed.

## Human acceptance checks still required

1. Use keyboard navigation from login through onboarding, marketplace, an app,
   Chat, and administrator forms. Check visible focus, error recovery, panel
   dismissal, and focus retention while lists update or access changes.
2. Repeat with macOS/iOS VoiceOver, Android TalkBack, and a Windows screen reader
   such as NVDA. Check names, headings, table relationships, status announcements,
   message ordering, and absence of repeated announcements on unchanged polls.
3. Test Safari and native WebViews, 200% text resizing and 400% browser zoom,
   device orientation, touch target comfort, forced colors, and reduced motion.
   The automated runs exercise desktop Chromium and simulated phone widths;
   they do not prove behavior on actual assistive technology or native devices.
4. Review organization-authored Markdown, custom themes/logos, and each deployed
   third-party application's content. The shared header supplies navigation;
   it cannot make an application's own controls or content accessible.

Record browser/device, assistive technology, task, expected behavior, and actual
behavior for failures. Recheck changed components and their consuming screens
before a release; no App Store enrollment or external account action is needed
for the web checks above.
