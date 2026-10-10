# Applications and shared integration configuration

An organization publishes integration contracts for external APIs, installed
applications, and Core services. Applications consume the same contract through
their backend whether they run in the Core-managed cluster or elsewhere. The
consumer's immutable organization, installation, and environment establish scope;
application names and endpoint URLs confer no authority.

Marketplace packages describe deployable software. An **integration definition**
describes the nonsecret configuration an application can consume, its provider
service, and its live role permission. A **platform connection** supplies settings
and an optional encrypted credential for one environment. An **application
integration** is the administrator's explicit approval for one installed consumer
in that environment. Publishing a definition never installs software, purchases a
service, calls a model, or grants consumption automatically.

## Choose where configuration lives

| Mode | Configuration and credential authority | Failure behavior |
| --- | --- | --- |
| Standalone | The application's own settings, authorization, and vault | No Core dependency |
| Platform | Core's application binding selects a shared platform connection or an app-specific configuration | Unavailable or revoked Core access denies use |
| Hybrid | The host explicitly routes selected integrations to Core and retains other local integrations | Selected Core integrations never fall back to local settings |

For Core-managed bindings, **Inherit platform connection** uses the current shared
connection; changes apply to the next authorized operation. **Application
configuration** uses this consumer's own settings and encrypted credential in Core.
An unused shared connection's policy does not restrict the app-specific connection.
Its availability and credential status are hidden unless both the app and current
administrator can access that shared connection.
**Disabled** denies consumption. No configuration dictionaries are implicitly
merged, and no credential is copied between sources when changing modes.

## Configure centrally and test it yourself

Start or update Core using [the local testing guide](local-testing.md). Sign in as
your organization administrator and open **Central admin → Applications and
integrations**. Use a sandbox environment first.

1. **Publish integration**: enter a stable slug such as `rdp`, a display name such
   as `[R]DP`, and a scalar JSON configuration schema. For an external API, choose
   the organization's Core Platform authorization service. For an installed app
   or Core service, choose its matching service, provider installation, and kind.
   The prefilled schema accepts an HTTPS `base_url` with an optional service path.
   Its identity and schema become immutable; publish a new definition for a changed
   contract. The provider application must already implement that contract.
2. **Configure platform connection**: select the published definition and sandbox,
   then enter actual nonsecret configuration, for example
   `{"base_url":"https://data.customer.example/api"}`. Enter credentials only in
   the separate password field if the provider requires them. Saving makes no
   external request. Keep bootstrap keys and database backups outside Git.
3. **Approve application consumption**: choose the consumer installation, definition,
   and sandbox. Select its platform connection, approve inheritance, and choose
   **Inherit platform connection**. Approve **Workload access** for the backend.
   Credential retrieval requires its own explicit grant; leave it off unless the
   trusted backend needs the key. All approvals default off.
4. Create or edit a role under **Users and permissions** and grant the displayed
   permission, such as `integration-rdp-use`, on the definition's authorization
   service. Assign it to the intended users or approved agent bindings. Assign
   required environment/classification tags as well. Administrators receive
   registered service permissions, but policy tags, MFA, and purpose still apply.
5. To permit configuration from the application's admin panel, enable **Allow
   application configuration**. Its backend presents its workload credential and
   the administrator's current Core-issued user access token. This delegation
   permits app-specific settings, disable, and already-approved inheritance;
   it cannot grant permissions, approve inheritance, select another shared
   connection, change the published schema, or edit the shared connection itself.
6. Confirm access through the SDK or APIs below. Disable the binding, connection,
   provider, environment, user membership, or role and repeat the request. Access
   must be denied immediately. Repeat with a different application or production
   credential; it must not see the sandbox binding. Review security audit events.

An app may be hosted separately and still register a marketplace installation
and an environment-scoped integration key. A Core installation does not require
deploying the app's container; existing workloads can use
[Kubernetes adoption](modernization.md) for automatic configuration injection.
A fully standalone application keeps its own auth and configuration and need not
register in Core.

## Backend contract for application settings

`CoreClient.from_environment()` reads the configuration injected by the platform.
An independently hosted backend can construct `CoreClient` using its trusted Core
URL, organization UUID, environment, and scoped key. Keep the key server-side.

| API below `/api/platform/v1` | Authorization and response |
| --- | --- |
| `GET /integrations/` | Only the calling app/environment's currently authorized integrations; status and nonsecret settings |
| `GET /integrations/<slug>/` | Resolve one enabled binding with live app, user/agent role, environment and tag checks |
| `GET /integrations/<slug>/settings/` | Delegated current organization administrator; schema, revisions and status-only credential flags |
| `POST /integrations/<slug>/settings/` | Same authority; optimistic revision and bounded schema validation |
| `POST /integrations/<slug>/credentials/` | Explicit backend consumption and credential grants; pinned binding and connection revisions; never available to agents |

Every API requires the calling backend/agent bearer credential. User-initiated
operations additionally send `X-Core-User-Token`; Core binds that token to the
calling installation, environment, and deployed instance when applicable. Both
the calling app and the user must satisfy resource policy; the user's identity
does not replace the app's restrictions. Background service-account use requires
an explicit workload grant. Agents require their own approved account, placement,
and the definition's service role permission. Task-scoped agents additionally
need this application's integration-binding UUID in `resources.integrations`.
Empty scopes deny access, including UAT.

Use the user's Core access token from the server's verified OIDC session:

```python
from core_client import CoreClient

core = CoreClient.from_environment()
current = core.integration_settings("rdp", user_access_token=administrator_token)
saved = core.configure_integration("rdp", {
    "revision": current["revision"],
    "mode": "application",
    "configuration": {"base_url": application_selected_url},
}, user_access_token=administrator_token)
```

The application must retain its own session, CSRF, administrator/owner, controlled
data, request bounds, and transport protections. A GitHub/Keycloak/cloud-provider
token cannot be placed directly in this header; use the Core-issued access token
after provider federation. A stale revision returns 409. Authentication failures
return 401, live policy/grant failures 403, missing app/environment bindings 404,
and invalid settings 400. Error responses do not echo submitted values.

## Safe per-operation configuration and credentials

Resolve before each operation; a saved descriptor is configuration, not an offline
authorization grant. Raw credential retrieval is a separate, server-only operation
with an explicit application grant. Pin the resolved revisions so a concurrent
settings change cannot supply a new endpoint's credential to an old operation:

```python
integration = core.resolve_integration("rdp", user_access_token=user_token)
credential = core.integration_credential(
    "rdp", integration=integration, user_access_token=user_token,
)
# The host's registered connector uses integration["configuration"] and credential.
# Do not send credential to a browser, prompt, run snapshot, log, or model tool.
```

Binding and connection locks protect the revision check and credential snapshot.
Changes require a new resolution; the client never automatically retries an
operation with another credential. Replacing/clearing credentials and definition,
connection, binding, and usage decisions have audit records without their values.
Password inputs and ordinary APIs expose credential status only. Changing a
credential-bearing configuration requires explicit replacement or clear to prevent
credential reuse with another destination. The encrypted vault uses Core's existing
deployment key; changing that key requires an operator-led vault migration.

Retrieval cannot recall a key already released to an approved backend. Downstream
services must enforce their own Core/OIDC authorization for immediate end-to-end
revocation, and external provider keys must be rotated at their provider when
necessary. Treat application backends receiving credentials as trusted. Core
does not fetch the configured URL or generate executable connector code.

## Standalone and hybrid application adapter

`integration_client.py` has no Django imports. It preserves an app's local role
checks and encrypted vault while adding optional Core routing:

```python
from integration_client import ApplicationIntegrations

integrations = ApplicationIntegrations(
    mode="hybrid", core=core, inherited={"rdp"},
    local_configurations=application_nonsecret_connection_lookup,
    authorize_local=application_live_role_check,
    local_credential=application_vault_lookup,
    configure_local=application_administrator_settings_update,
)
configuration = integrations.resolve("rdp", user_access_token=user_token)
```

The host supplies trusted callbacks and the authenticated actor, never a model or
browser-supplied identity. Local authorization must return the boolean `True` for
the named `use`, `configure`, or `credential` operation. Supply a configuration
lookup callback `(slug, actor)` to read current local settings for each operation;
a dictionary is also supported for fixed settings. Local persistence/schema,
vault snapshots, CSRF, and ownership remain the app's responsibility.
`ApplicationIntegrations.from_environment()` chooses platform mode when injected
`CORE_SERVICES_URL` exists and standalone otherwise; explicit mode overrides are
available. Hybrid routing requires explicit inherited names. Standalone mode
works without Core, network access, a provider account, or a paid service.

## Proposal Studio mapping and limits

Reviewed `NorthCountryEngineer/Proposal_Studio` at
`a944695cb62f2ac69b0991818859bcaeed5cee98`, specifically its administrative settings,
connector, model-catalog and organization documentation. It already keeps an
organization-owned connector catalog and encrypted, status-only vault and can
run independently. Map the shared contract into **Administrative settings →
Integrations → Configured integrations**, using its local implementation for
standalone connections and the delegated settings API for centrally managed
ones. Preserve its owner-bound credentials, immutable operation snapshots,
network/controlled-content protections, and model publication/usage accounting.

This change supplies Core's admin UI, consumption/configuration APIs, and portable
adapter. Proposal Studio's source and UI have not been modified; wiring that
existing UI and connector execution to these SDK methods remains an application
adapter change. No proprietary `[R]DP` endpoint contract or live instance was
provided, so no vendor-specific routes or production integration are claimed.
Applications must implement their provider contracts and enforcement; publishing
a definition or injecting environment variables does not rewrite application code.
Integration use grants authorize configuration consumption; the provider's own
data, chat, model, workflow, and application-operation permissions still apply.
No remote service, real credential, paid model, or customer cluster was used to test
this feature. These controls do not establish HIPAA compliance by themselves.
