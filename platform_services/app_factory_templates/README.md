# Generated application

This is an executable, bounded data application with an organization login, browser forms,
read/create JSON APIs, live Core authorization, collection permission tags, and operation accounting.
No prompt or specification is executed as code. Review `REQUEST.md`, `app-spec.json`,
`security-contract.json`, and source files before building. Description text is context;
the reviewed structured specification defines the generated behavior.

Run `bash ci/check.sh` to execute generated security/runtime tests. Build a container
using `docker build -t your-registry/your-app:reviewed .`, then publish through your configured
CI provider and record its immutable image digest and test evidence. Building or publishing
on paid infrastructure requires your own cost approval. The included GitHub workflow is manual;
any CI provider can invoke the same check command and Docker build.

In the Core App Factory request, review the source digest, image digest, and test evidence
to release a private marketplace application. Use its central deployment controls to select
an environment, runtime, and HTTPS endpoint. Core injects its OIDC client credentials,
organization/service/deployment IDs and secret-file paths; no shared password is included.
Assign `app-read` and `app-write` to the intended human or agent service roles. Every collection
is a registered `app_collection` resource; administrators manage its policy tags in Core.
Sign in through the generated UI. Browser authentication uses OAuth authorization code + PKCE,
server-side short sessions, HTTP-only cookies, and CSRF protection. Backend clients can send
service-bound bearer tokens to `/api/records/<collection>`.

The service denies operations if Core is unavailable or permissions, placement, or tags are revoked.
Sessions are process-local: this starter deliberately uses one worker; restarts require login.
The starter uses `/tmp/app.sqlite3`. For durable storage, set `APP_DATABASE=/data/app.sqlite3`,
mount a writable persistent volume at `/data` owned by UID/GID 10001, and use a read-only
root filesystem. The default Core deployment has ephemeral storage: configure persistence before
storing important data. Accounting is metadata-only SQLite storage, not a tamper-proof retention system.
Core also records policy decisions centrally. These controls do not establish regulatory compliance.

Local development uses `APP_DATABASE=/tmp/app.sqlite3` and `python server.py`, with platform
deployment variables and secret files supplied by the operator. HTTP loopback additionally requires
`CORE_ALLOW_LOCAL_HTTP=1`; production uses HTTPS. Agent bearer tokens use live Core agent
introspection, service role permissions, this application's approved placement, and collection
task scope. Supply `X-Core-Task-ID` for task-bound agents. Agents have distinct accounts and no
browser-session impersonation.

`integration_client.py` supplies the existing standalone/platform/hybrid integration adapter;
integrations require explicit platform/app bindings and RBAC. No remote integration is enabled
automatically. No model provider, container build, registry publication, or Kubernetes deployment
is initiated by source generation. Custom logic, update/delete operations, fine-grained field/record
policies, distributed sessions, or production promotion require a reviewed extension.
