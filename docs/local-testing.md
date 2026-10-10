# Run and test Core Platform on your Mac

The Codex cloud checkout and your Mac are separate machines. Start these
processes in your own NCE_Core checkout. Use `uv` and Python 3.12 or 3.13;
`brew install uv` installs uv if you use Homebrew and don't have it yet.

## Start or update

To test shared connections and application inheritance, follow
[Applications and integrations](application-integrations.md#configure-centrally-and-test-it-yourself)
after starting the platform and establishing your organization below.

```bash
git pull origin main
uv sync --frozen
uv run python scripts/init_dev.py
CORE_DEBUG=1 uv run python manage.py migrate --noinput
CORE_DEBUG=1 uv run python manage.py sync_platform_docs
CORE_DEBUG=1 uv run python manage.py check
```

If needed, first clone `https://github.com/NorthCountryEngineer/NCE_Core.git` and
`cd NCE_Core`. Preserve local changes if Git reports conflicts. Initialization
creates unique keys under ignored `.local/` only when missing and preserves
your database. No default account or password is installed.

Check whether it is already running:

```bash
curl -fsS http://localhost:8000/health/
```

Expect `{"status":"ok"}`. After updating, restart your own web/worker terminals
with Ctrl-C so they load new code. If port 8000 belongs to something else, inspect
it with `lsof -nP -iTCP:8000 -sTCP:LISTEN`; don't kill an unfamiliar process.

Create an operator account once, if you don't have one:

```bash
CORE_DEBUG=1 uv run python manage.py createsuperuser
```

Enter your own email and unique password at the prompts. For an existing account,
`CORE_DEBUG=1 uv run python manage.py changepassword YOUR_EMAIL` resets its password.

Terminal 1, from the checkout:

```bash
CORE_DEBUG=1 uv run python manage.py runserver 127.0.0.1:8000
```

Terminal 2, from the same checkout:

```bash
CORE_DEBUG=1 uv run python manage.py core_worker
```

Keep both running. The worker handles simulations/workflows and service health;
runserver doesn't start it automatically. Stop each with Ctrl-C when finished.
Use this server locally; development settings are not an Internet deployment.

## Establish your organization

1. Open `http://localhost:8000/accounts/login/` in your Mac browser and sign in.
2. Open `http://localhost:8000/onboarding/`. Superusers can enter directly. To test
   automatic onboarding for an ordinary account, create it in platform account
   administration and run `CORE_DEBUG=1 uv run python manage.py grant_developer YOUR_EMAIL`.
3. Create **Sandbox**, slug `sandbox`, and choose its product display name.
4. Supply organization metadata; choose **Configure later** for billing.
5. Choose **Sandbox**, namespace `sandbox-sandbox`, application origin
   `http://localhost:8000`, and leave automatic deployment unchecked. Ingress/TLS
   can be blank for this exercise.
6. Leave runtime name blank to defer Kubernetes, then review and activate.
7. Open `http://localhost:8000/organizations/sandbox/platform/`. Core service
   health should update while the worker runs.

Operator administration is at `http://localhost:8000/platform-admin/`; use it
for accounts and marketplace publication. Use the organization panel for roles,
agent accounts, data, chat, training, and federation. These steps create no
cluster, charge, or paid model call.

For a member to create chat conversations, explicitly grant `core-chat: chat-manage`
in addition to read/send permissions. Follow [Chat testing and deployment](chat-testing.md)
for the complete role and deployment test walkthrough.

## If login reports a CSRF origin error

If a form reports `Origin checking failed - null`, update to the latest `main`,
restart the web server, and open `/accounts/login/` again in a fresh tab. Avoid
resubmitting the old form from the browser's Back button. Core uses the
`same-origin` referrer policy so browser forms retain their origin while external
links receive no referrer. CSRF tokens and origin validation remain required;
do not trust the `null` origin or disable CSRF protection.

## Environment and modernization exercise

For an existing organization, keep its current environment and add any missing
ones through **Sandbox, development, and production**. New organizations above
start in Sandbox. Create **Development** with namespace `sandbox-dev` and
**Production** with namespace `sandbox-prod`; use an HTTPS origin for Production.
These are configuration records and do not deploy anything.

1. Create separate sandbox and production datasets and assign their matching
   environment boundary tags through **Tags**. Create an application integration
   key for Sandbox, grant `data.read` on its installation, and verify it can read
   sandbox records while production and unassigned datasets return HTTP 403.
2. Disable Sandbox in environment configuration and repeat discovery/data access:
   the key should be denied immediately. Re-enable it to continue.
3. Run the automated checks locally:

   ```bash
   CORE_DEBUG=1 uv run python manage.py test platform_services.tests.test_modernization
   ```

For a Kubernetes application, follow [modernization.md](modernization.md): review
and apply the namespace isolation/RBAC bundles on your existing cluster, connect
an environment runtime, then choose **Modernize a Kubernetes application** in
Central admin. Confirm that its image, replicas, sidecars and storage survive the
integration rollout. Kubernetes network isolation needs a CNI that enforces
NetworkPolicy; verify cross-namespace traffic is denied on your cluster.

Existing manual keys must be replaced with environment-scoped keys after
migration. Tag existing datasets, conversations and other business resources
before using those new keys. Your existing configuration becomes its initial
environment; it is not silently renamed to Sandbox.

## First training exercise

For description-led application development, use the separate
[App Factory walkthrough](app-factory.md#try-it-yourself) from the central panel's
**AI application creation** link. Keep both the web process and Core worker running.

1. Create dataset `sim-input` with **Synthetic / training** classification and:

   ```json
   {"type":"object","required":["value"],"properties":{"value":{"type":"integer"}}}
   ```

2. Create pipeline `sim-input` into it, transformations `[]`.
3. In **Marketplace → Agents**, install **Training Coordinator**. Create role
   **Training runner** with `training-run` on Core Training and `data-write` on Core Data.
4. Assign the **environment-sandbox** boundary tag to the dataset and agent
   definition using their **Tags** links in Central admin. Open the agent account
   and approve consumption through **Core Training** in **Sandbox**, assigning that role. Leave task blank for this exercise.
5. Open **AI training harness**, create a scenario, select the pipeline and agent
   approval, leave workflow blank, and set goal **Check integer ingestion**. Plan:

   ```json
   {"seed":42,"cases":[{"name":"integer-input","count":5,
     "fields":{"value":{"kind":"integer","min":1,"max":10}},
     "expected_ingest":"accept",
     "assertions":[{"field":"value","operator":"type","value":"integer"}]}]}
   ```

6. Run and refresh its result page. Expect **completed**, five events, five
   passing evaluations, and the agent UUID. Add review feedback if desired.
7. Run again: seeded values should match, with a different run ID/provenance.
8. Disable the agent approval and try another run: it should be denied.
   Operational datasets should also be rejected as training destinations.

This uses the real data pipeline under the agent's own permissions. AI provider
tools are documented in [training.md](training.md); paid inference is not enabled.

## Permission, chat, and federation checks

- Invite a second ordinary user, sign in through a private browser window, and
  verify it cannot enter administrative controls. Grant a specific role and
  check that permission changes take effect without restarting.
- Add that user to a chat conversation and send messages from both browsers.
  Remove its participation and check that history access is denied.
- AI chat needs an agent participant, conversation AI enabled, participant AI
  approval, and the agent's `chat-send` / `chat-ai` permissions.
- Restrict a dataset/conversation with a permission tag; users/applications without
  tag grants should be denied. Native login cannot satisfy an MFA-required tag.
- For federation, start with `CORE_DEBUG=1 uv run python manage.py test core_fabric.tests`.
  It starts an independent temporary Core instance/database and exercises actual
  administrator approvals, selected fields/rows, scoped writes, and revocation
  from either side. Test instances stop afterwards; no persistent test accounts
  are added to your development database.
- For your own persistent pair of deployments, follow [data-fabric.md](data-fabric.md).
  Use distinct issuers/databases, dedicated peer credentials, matching agreement
  terms, and approval by both administrators. Start with read-only scope.

## Automated checks and troubleshooting

```bash
CORE_DEBUG=1 uv run python manage.py test platform_services.tests.test_training core_fabric.tests
CORE_DEBUG=1 uv run python manage.py test identity.tests platform_services.tests core_chat.tests core_fabric.tests
CORE_DEBUG=1 uv run python manage.py makemigrations --check --dry-run
```

Tests use temporary databases. Live Kubernetes checks are optional; model-provider
keys and paid infrastructure are unnecessary. If work stays queued, check terminal
2. Missing-table errors mean stop your own processes, migrate, then restart.

For a defect, report the page/action, organization slug, expected versus actual
result, and relevant web/worker error text. Exclude passwords, tokens, mounted
credential contents, and sensitive records. Screenshots help for visual defects.
