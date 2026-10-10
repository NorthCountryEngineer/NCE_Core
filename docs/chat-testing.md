# Test and redeploy Core Chat

Core Chat runs inside the Core Platform web process. Its monitored embedded
instance receives health reports from the separate Core worker. Updating Chat
therefore updates Core's web image/process; a marketplace toggle alone does not
reload its Python code. This guide covers a local Mac and disposable container
deployments without provisioning a cluster, purchasing services, or invoking AI
providers.

## Give a member access to create conversations

`chat-read` and `chat-send` allow a member to use conversations shared with them.
They do not grant creation. The UI hides the Create conversation link until the
organization administrator explicitly adds `chat-manage` to that member's role.

1. As administrator, open your organization at
   `http://localhost:8000/organizations/YOUR_ORGANIZATION_SLUG/`.
2. Under **Roles**, open **Chat User** (or your existing chat role).
3. Select `core-chat: chat-read`, `core-chat: chat-send`, and
   `core-chat: chat-manage`. Use Command-click to select multiple entries on a Mac.
4. Save. Check the test user's membership has this role, **Member**, and **Active**.
5. In the test user's separate/private browser window, refresh **Open chat**.
6. Choose **Create conversation**, enter a title, keep **Active** checked,
   leave federation fields blank, and save.
7. The creator becomes the conversation's owner. **Participants and permissions**
   lets them share their own conversation with existing organization members.

The management grant does not grant platform administration, marketplace
installation, organization role assignment, or control of another owner's room.
Read/send-only users continue to require administrator/owner sharing. A reader
participant remains unable to send even when their service role allows sending.

## Update the running Mac instance

Stop your own web and worker terminals with Ctrl-C. In your existing checkout:

```bash
git pull --ff-only origin main
uv sync --frozen
CORE_DEBUG=1 uv run python manage.py migrate --noinput
CORE_DEBUG=1 uv run python manage.py sync_platform_docs
CORE_DEBUG=1 uv run python manage.py runserver 127.0.0.1:8000
```

In a second terminal from the same checkout:

```bash
CORE_DEBUG=1 uv run python manage.py core_worker
```

Reload the test user's Chat page after updating its role. Preserve your `.local/`
keys and database; no account/password reset is needed. The Codex cloud workspace
and your Mac are separate machines. Restarting one does not redeploy the other.

## Backend and portal regression

Use your local development environment:

```bash
CORE_DEBUG=1 uv run python manage.py test core_chat.tests identity.tests.test_csrf --noinput
```

This includes native form/CSRF tests, granular roles, owned-room management,
live service/membership/participant/permission revocation, tenant isolation,
permission tags and MFA boundaries, attribution, cursor reads, size/validation,
idempotent messages, consumer credentials and user delegation, agent accounts,
AI approval gates, and real HTTP federation between independent instances.
The Django test runner uses a separate test database. Do not point it at a
customer production database.

## Cold deployment and real browser suite

Requires a local Docker engine, Python 3.12 or 3.13, and at least 3 GiB of free
Docker storage after building the image. The suite checks space before starting
its database; Docker's VFS driver copies image files for each container. The pinned optional
`browser` dependency group keeps browser tooling out of the production image.

```bash
uv sync --frozen --group browser
uv run --group browser python -m playwright install chromium
uv run --group browser python ci/chat_deployment.py --build --browser chromium
```

On Linux, provision Playwright's documented system libraries on the runner first.
An existing system Chromium can be selected with `--browser-executable /path/to/chromium`.
To test Safari's WebKit engine on a Mac:

```bash
uv run --group browser python -m playwright install chromium webkit
uv run --group browser python ci/chat_deployment.py --build --browser chromium --browser webkit
```

For an already built local Core image, omit `--build` and supply `--image IMAGE`.
The suite never publishes that image. Docker builds use the existing CA/proxy
configuration supported by `CORE_BUILD_CA_FILE`, `CORE_BUILD_NETWORK`, and
`CORE_BUILD_PROXY_HOST`; TLS verification remains enabled.

The suite creates a private Docker network, disposable PostgreSQL, mounted fresh
keys, a migration job, non-root read-only web and worker containers, and a
loopback-only HTTP port. It runs backend regressions inside the same image against
PostgreSQL. All browser users and organizations are synthetic. Passwords are
generated in memory and passed through standard input, never command arguments
or reports. Runtime secrets stay in a private disposable volume and database
container metadata until cleanup. Your local `.local/` database, accounts and
keys are not used.

Separate administrator and member browser sessions exercise:

1. Native login, redirect, and all five onboarding steps.
2. Marketplace activation and live Chat disable/re-enable.
3. Invitation acceptance, explicit role assignment, and no implicit permissions.
4. Read/send-only membership, hidden creation controls, and direct-route denial.
5. Shared conversations, real sender attribution, polling, and preserved drafts.
6. Escaped HTML in initial and polled messages; duplicate sends produce one record.
7. Rejected null/foreign origins and missing CSRF tokens.
8. Live send revocation, reader participants, and forged-write denial.
9. Explicit creation grants, member-owned rooms, and denied platform/other-room administration.
10. Participant/membership/service revocation clears displayed history and disables sending.
11. Logout/login, fresh worker heartbeats, and durable history after web/worker restart.

Any failed assertion exits nonzero. `artifacts/chat-deployment.json` records the
image ID/revision, source state, executed browser checks, backend count and result,
without credentials, invitation links, browser traces, or database contents.
Containers, database volumes, the secret volume, and the private network are
removed on success and failure. The built local image remains reusable.

## CI/CD configuration

Backend and portal tests are included in the existing portable pipeline's regular
test stage. On a runner with Docker and browser system libraries, set
`CORE_CI_CHAT_DEPLOYMENT=1` and optionally `CORE_CI_CHAT_BROWSER=chromium`, `firefox`,
or `webkit`. `ci/run.sh` installs the pinned browser group and selected browser;
the shared pipeline tests its built image before publication/deployment. A failing
Chat deployment test prevents that pipeline from advancing to release.

This switch is opt-in and does not enable registry publication, external
deployment, hosted CI triggers, or paid runners. The existing seven platform
adapters call the same pipeline. Review and approve any runner/infrastructure
costs before enabling hosted execution.

This is a container deployment suite with local HTTP development configuration.
It does not attest a customer's HTTPS ingress, DNS, Kubernetes networking,
production identity provider, or registry access. Those remain the configured
release pipeline's smoke checks and connected-cluster tests.
