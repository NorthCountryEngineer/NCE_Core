# AI-led application creation

Released applications include an `APPLICATION.md` manual for their reviewed collections,
field types, APIs, security, deployment, and storage limits. Human marketplace release
publishes a versioned documentation source; the organization's platform inherits it immediately.
Use [Organization documentation](organization-documentation.md) to add local guidance and
assign documentation roles. The archive includes the portable documentation adapter; reading
Core documentation from the generated backend requires explicit `docs.read` approval.

Core App Factory is a consumable marketplace service (`core-app-factory`). It takes an
application description and a reviewed structured specification, then coordinates six
separate development-agent accounts through the durable Core workflow engine. It produces
an executable web service, source archive, tests, a container definition, and a private
marketplace release handoff. It does not enable a paid model provider or infrastructure.

The first generator supports data applications with 1–8 collections and 1–20 scalar
fields per collection. It generates read/create APIs and browser forms. An AI can translate
a natural-language description into the specification through the function tools below;
Core itself currently uses trusted templates and deterministic development tools. A freeform
description alone does not produce arbitrary business logic. Custom code generation and
execution need a separately configured model and isolated runner; no host shell tool is exposed.

## Try it yourself

1. Follow [local testing](local-testing.md). Pull the latest main, apply migrations, and
   run both the web service and `manage.py core_worker`. Existing Core organizations receive
   the new marketplace service through the migration; new onboarding installs it with the others.
2. Open your organization's central panel and select **AI application creation**.
3. Create **Review tracker**, describe the app, and choose your registered sandbox environment.
   Review this specification (or have your AI produce a different supported specification):

```json
{
  "resources": [
    {
      "slug": "reviews",
      "name": "Reviews",
      "fields": {"title": "string", "status": "string", "approved": "boolean"}
    }
  ]
}
```

4. Assign appropriate permission tags to the request before approval. A tag that excludes
   machine access blocks development agents. MFA/purpose/classification requirements remain
   effective; organization administrators cannot bypass them.
5. Select **Approve specification and scoped development team**. Planning, backend, frontend,
   security, test, and packaging stages each use a distinct account and project-scoped task.
   Refresh to see completion and each account's step trace. The specification is immutable after
   approval; revisions require a new request. Approvals expire after 24 hours. Administrators can
   renew the task through agent task controls after reviewing it, or revoke any binding/account.
6. Download the source archive into an isolated development directory. Inspect `REQUEST.md`,
   `app-spec.json`, `security-contract.json`, and the source. Run `bash ci/check.sh` with uv/Python 3.12.
   The same command works with any of the platform's [CI/CD targets](cicd.md). A manual GitHub
   workflow is included. Source-generation success does **not** mean these tests have executed.
7. Build/publish the reviewed image using your configured CI/registry. Core never starts a build,
   model call, registry publication, or deployment during source generation. Approve any paid
   infrastructure/provider use yourself before running it.
8. Enter the exact source digest shown by Core, the digest-pinned image reference, and a reference
   to reviewed successful tests/provenance. A current human administrator approves the private
   marketplace release. The evidence reference is an explicit human attestation; Core does not
   independently verify a CI signature or that an arbitrary image contains this source.
9. The application appears in the central panel. Assign `app-read`/`app-write` service roles to
   the intended users and agents, then select its environment, runtime, and endpoint through
   existing deployment controls. Core injects identity and integration configuration.
   Deployment is a separate reviewable action and may use paid infrastructure.
   New generated services also inherit [Core AI rules](ai-rules.md): explicitly approve
   `rules.read`, `usage.reserve`, `usage.report` on the installation and assign intended
   users/agents Core Platform `rules-read`, `usage-reserve`, `usage-report`. Configure
   the injected backend workload token/services URL. Missing rules connectivity or
   grants denies reads/creates; shared quotas follow the native user across apps.

Builds inherit their development environment tags. Initial releases retain these tags on
the installation and registered collection resources, so cross-environment use is denied.
This version does not automate production promotion. Promotion requires operator review of
source/image provenance, deployment placement, environment tags on the installation/resources,
persistence, network policy, and intended consumer grants. Do not remove an environment tag
simply to make an authorization error disappear.

## Platform security inheritance

The generated service uses the platform's OIDC authorization-code flow with PKCE. Core brokers
native, Keycloak, or cloud-provider login; the app does not store user passwords or implement
another identity authority. Its short browser sessions retain bearer tokens only in server memory,
use HTTP-only/SameSite cookies, and protect writes with CSRF tokens. Sessions require one worker
and reauthentication on restart. No browser token is stored in local storage.

Every business operation makes a live Core permission check. `ResourceClient` validates issuer,
tenant, audience, service, expiry, deployment, and environment. Collection reads require `app-read`;
creates require `app-write`. Each collection is an `app_collection` `ProtectedResource` with
inherited environment and classification tags. Unknown collections, missing permissions,
revoked credentials, tag denials, and Core outages fail closed. This is collection-level policy;
field- and record-specific policies require a reviewed extension.

Agent bearer tokens use the existing live agent-introspection API, approved application placement,
service roles, environment, expiry, and registered-resource task scope. Task-bound agents send
`X-Core-Task-ID`. They never borrow a human identity. Generated operation accounting records
subject, collection, operation, outcome, and time without record contents or credentials.
Core policy decisions are centrally audited. Local SQLite accounting is not tamper-proof archival
retention, and these controls do not establish HIPAA or other regulatory compliance.

Build agents can operate on only their approved project in a non-production environment.
They receive tool-specific definitions and only `agents-run`/`appfactory-build` permissions.
They cannot release images, deploy, charge, access customer datasets, read provider secrets,
or execute arbitrary code. Before every step, the worker rechecks the submitter's administrator
membership, each account/binding/listing/task, live roles and tags, and specification digest.
Development tools accept only their authorized build's preceding stored output, so callers cannot
inject source files or forged stage results. Source export/release also recheck these approvals
and the complete artifact digest. Failed builds remain visible in the normal workflow panel.

The generated starter uses bounded SQLite storage under `/tmp`, which works with the Core
connector's read-only root filesystem and ephemeral scratch mount. It is suitable for evaluation.
Configure durable volume storage and retention before storing important data. The reference
runtime uses a single replica/worker; distributed sessions/database scale are extensions.

The existing standalone/platform/hybrid [integration adapter](application-integrations.md)
is included. Integration bindings, inherited provider configuration, and credentials still need
explicit platform/application grants; source generation enables no remote connector automatically.

## AI and application API

App Factory accepts the existing workload/agent authentication and live service permissions:

| Endpoint | Capability | Result |
| --- | --- | --- |
| POST `/api/platform/v1/app-factory/projects/` | `appfactory.create` | Draft description/specification in the authenticated environment |
| GET `/api/platform/v1/app-factory/projects/<uuid>/` | `appfactory.read` | Authorized draft/build/release status |
| GET `/api/platform/v1/app-factory/projects/<uuid>/source/` | `appfactory.read` | Authorized ZIP and `X-Core-Source-Digest` |

Approval and release are human-admin browser actions protected by CSRF. AI tools cannot approve
their own work or select another tenant/environment. Treat descriptions as untrusted data.
Do not put credentials or regulated customer records into requests or prompts.

```python
from core_client import CoreClient
from app_factory_client import AppFactoryTools

core = CoreClient.from_environment()  # Explicit app capability grants; secret file on backend
factory = AppFactoryTools(core, core.environment)
function_schemas = factory.tools()   # Supply to your configured AI provider
# Dispatch a returned function call only through the host's approved tool registry:
result = factory.execute("core_app_create", {
    "name": "Review tracker",
    "description": "Track review requests and approval status.",
    "specification": {"resources": [{"slug": "reviews", "name": "Reviews",
        "fields": {"title": "string", "approved": "boolean"}}]},
})
```

The provider/model is chosen and invoked by your application's AI host. The portable function
schemas work with OIDC-authenticated workloads and existing Core agent grants. Use a narrowly
scoped application integration key, not an organization administrator token. An existing task-bound
agent needs an explicitly approved `app_projects` resource list to read a project. Newly created
drafts require administrator review and a scope update before such an agent can read them.
