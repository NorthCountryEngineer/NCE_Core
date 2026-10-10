# Organization and application documentation

Core Platform hosts an organization documentation library for humans and AI. Marketplace
applications publish versioned Markdown guides; installing an application inherits those guides
into the organization's library. Core's own administration, identity, deployment, integration,
CI/CD, chat, agent, workflow, training, data fabric, and App Factory manuals are included.
The organization getting-started guide covers everyday access and support. The existing guides
describe implemented behavior and operational limits; publishers remain responsible for documenting
their application's actual features, API, recovery procedures, and release changes.

## Start reading and testing

1. Initialize the existing development environment and migrate its database as described in
   [Local testing](local-testing.md). Then run `CORE_DEBUG=1 .venv/bin/python manage.py sync_platform_docs`.
   Cold database migrations publish initial bundled sources. Synchronization publishes changed
   bundled sources as new revisions, adds missing inherited pages, and preserves existing local edits
   and pinned source choices. The command is safe to repeat.
   The shared CI/CD deployment pipeline also runs documentation synchronization after successful
   production checks and migrations, before updating web and worker deployments.
2. Sign in and select **Organization documentation** from your organizations page, or
   **Organization and application documentation** from the central admin panel.
3. Open a guide, search by title/content, or select an environment to render its context.
   Each page shows the installed package version, publisher revision, organization revision,
   SHA-256 content digest, and stable citation. Download Markdown, JSON, or AI chunks in JSONL.
4. In documentation administration, add a guide or edit an inherited page. Choose **inherit**,
   **add organization guidance**, or **replace with organization guidance**. A source update is
   deliberately reviewed before adoption. Existing content and source choices are preserved.
5. Use **Permissions and classification tags** on a page to restrict its readers. Validate with
   an ordinary member and an environment-scoped application or agent account, including denial
   after removing a role or tag grant. The API fails closed when Core cannot authorize the caller.

## Placement and inheritance

Each organization page has an immutable placement: optional installation, environment, and
deployment, plus a slug. An installation must belong to the organization; a deployment must match
that installation and environment. Organization-wide instructions have no installation. Shared
application guides have an installation and no environment; these are intentionally generic
instructions, and reading them grants no access to data or operations in other environments.
Create a separate scoped guide when instructions differ between sandbox, development, UAT, or
production. Scoped pages appear alongside shared guides, clearly labeled; they do not silently
shadow shared pages with the same slug. Disabling a page prevents retrieval.

Publisher revisions are immutable and scoped to a package version. Organization pages pin a
specific publisher revision. New source revisions never replace local extensions or replacements
automatically. Following an installed-version upgrade, old-version inherited pages stop serving
until an administrator chooses a matching source. The administration list flags updates and
withdrawals for review and shows installations with no registered guides. Source withdrawals,
disabled applications/services, inactive environments, digest mismatches, and live tag/role
revocations prevent reads, search matches, and exports. Source policy still applies to replacement
pages: changing local content cannot downgrade a technical source into a public guide.

Every edit creates an organization revision snapshot with a digest; concurrent edits require
reloading the current revision. Source and page revision history is retained for review by platform
operators and organization administrators. Historical snapshots are not exposed through the AI
reader. Deleting applications with documentation requires deliberate archival because provenance
relations are protected. Back up documentation tables and their policy relations with the platform
database as described in [Deployment operations](deployment.md).

## Organization context

Markdown supports a fixed, non-executable placeholder vocabulary:

In publisher Markdown, put an exclamation mark immediately after the opening braces to
display literal placeholder syntax in examples, as this guide does. Rendering removes that
marker without substituting the value; ordinary placeholders substitute the authorized context.

| Placeholder | Value |
| --- | --- |
| `{{!organization_name}}`, `{{!organization_id}}` | Current organization |
| `{{!platform_name}}` | Organization's configured Core identity/product name |
| `{{!application_name}}`, `{{!application_version}}`, `{{!service_id}}` | Installed application/service |
| `{{!environment_name}}`, `{{!namespace}}` | Authorized environment context |
| `{{!deployment_id}}`, `{{!application_origin}}` | Authorized deployment; otherwise selected environment origin |
| `{{!support_email}}` | Organization profile support address when its policy permits access |

Templates execute no code, perform no remote fetches, and include no billing records, keys,
integration credentials, or arbitrary profile metadata. Application/agent context comes from the
live approved credential, not model-supplied parameters. Human readers can select their authorized
environment/deployment. Organization values are escaped before Markdown rendering; HTML is
sanitized, and scripts, iframes, images, event handlers, and unsafe link protocols are removed.
Relative links between bundled manuals resolve only to accessible organization pages. A content
digest includes the rendered context and provenance, so it changes when organization context changes.

## Publish an application's documentation

An organization administrator can publish sources for its private marketplace packages through
**Publish application source**. Shared marketplace publishing requires a platform superuser.
Publication is an explicit JSON page bundle; Core does not crawl publisher URLs or ingest remote
repositories automatically. Example, with the actual package's release version:

```json
{
  "version": "0.1.0",
  "slug": "getting-started",
  "title": "Application guide",
  "summary": "Sign in, use the application, and obtain support.",
  "category": "getting-started",
  "audience": "users",
  "required_permissions": [],
  "markdown": "# {{!application_name}} for {{!organization_name}}\n\nSign in through {{!platform_name}}. Contact {{!support_email}}."
}
```

Pages are limited to 128 KiB of source or local Markdown. Categories include getting-started,
user-guide, administration, api, operations, security, architecture, troubleshooting, and
release-notes. Audience is `users` or `administrators`; optional `required_permissions` lists up to
20 registered permission codenames on the documented service. Publishing adds missing inherited
pages to existing installations but never changes their pinned revisions. Bundle pages for
users, administrators, APIs, installation/integration, deployment, monitoring, backups/recovery,
troubleshooting, security boundaries, and release notes as appropriate. Avoid credentials,
regulated records, and actual customer examples in documentation. Use synthetic examples.

App Factory's package agent includes an application-specific guide in every reviewed source
archive. Human release publishes that guide for the generated private package before installation,
so the organization's platform immediately inherits it. Its guide documents the actual generated
collections, field types, read/create APIs, roles, deployment/storage limits, and existing platform
integration. Custom applications and existing Kubernetes workloads must register their guides;
an image alone does not provide trustworthy application documentation. Standalone apps can ship
the same Markdown locally; consuming an organization's personalized library requires explicit
Core registration, approved credentials, and an available Core authority.

Generated applications also include a guide search in their own UI and `/api/documentation`
for authenticated application consumers. Approve `docs.read` on the installation and grant Core
and application documentation roles to activate it. Native users use bound OAuth delegation;
agents use their own approved account/task. Opened guides are fetched with fresh authorization
and rendered as reference text with citations in the starter UI.

## Permissions and AI retrieval

Core Platform service roles use `docs-read`, `docs-admin`, and `docs-manage` (API capability names
`docs.read`, `docs.admin`, `docs.manage`). Members need `docs-read` to read user guides; technical
guides also need `docs-admin`. Human documentation management additionally requires current
organization administrator membership. Application-specific human readers and agents need that
application's `docs-read` permission, plus any source/page additional permissions. This permission
is registered at installation; administrators decide which roles receive it. A documentation
role does not grant the application's business operations.

Application backends need an installation manifest declaring `docs.read`, an approved capability
grant, and an environment-bound workload credential with the necessary live tag grants. Technical
readers additionally need `docs.admin`. Workload credentials can read approved pages with no
additional end-user permission requirement. For user-role-protected pages, pass a service-bound
OIDC/OAuth access token in `X-Core-User-Token`; both workload and human policies are checked. A
token from another service, deployment, or environment is rejected. Revocations apply immediately.
Agents use their own account, live service roles, approved consumer/environment binding, and
task scope (`documentation: [page UUIDs]`); they receive no publishing or editing tool.

| API | Result |
| --- | --- |
| `GET /api/platform/v1/documentation/?q=term&limit=20&after=UUID` | Authorized rendered pages and next cursor |
| `GET /api/platform/v1/documentation/PAGE_UUID/` | Markdown with organization context, source metadata, revision, digest, citation |
| Same page with `?format=markdown` | Markdown attachment |
| Same page with `?format=chunks` | Structured AI chunks |
| Same page with `?format=jsonl` | One AI chunk per line |

Search limits are 1–100 results, at most 200 candidate rows per request and about 1 MiB of rendered
document JSON before envelope overhead. Follow the cursor even if a bounded scan returns no matches.
Placeholder expansion is capped at 300,000 bytes of rendered Markdown; split oversized guidance into
separate pages. Non-printing control characters are rejected.
All results are authorized before matching content; inaccessible titles/snippets/counts are omitted.
Machine readers consume their own application's documentation, shared guides for installed core services, and
organization-wide guidance within their approved environment/deployment. There is no cross-tenant
documentation federation enabled implicitly. Responses are private/no-store. Audit records contain
actor, policy decision, resource and purpose metadata, not document bodies or search text.

```python
from core_client import CoreClient
from documentation_client import OrganizationDocumentation

docs = OrganizationDocumentation(CoreClient.from_environment())
results = docs.search("backup", limit=10)
for page in results["documents"]:
    print(page["title"], page["citation"])
    text = docs.read(page["id"])["markdown"]
    # Supply text and citations as reference context, subject to host policy.
```

The adapter validates organization, environment, and response digests. `tools()` exposes only
`core_docs_search` and `core_docs_read`; `execute()` dispatches those functions for any AI provider.
The host fixes Core URL, identity, credentials, user delegation, purpose, and deployment context.
AI references are explicitly labeled `reference_data_not_tool_authority`: instructions in a guide
cannot grant access, change tools, approve deployment, or authorize messaging or spending. No
model provider, paid indexing service, embedding generator, or remote fetch is invoked. JSONL
chunks include stable IDs, citations, rendered digest, source metadata, revision, and context;
exported files and downstream indexes require their own access/retention controls. Once delivered,
an export cannot be recalled by a later Core revocation. Reauthorize fresh retrievals and delete
or rebuild external indexes when access or source versions change.

## Troubleshooting

- **401**: missing/revoked application or agent credential, or invalid delegated OAuth token.
- **403**: missing Core/app documentation role, technical audience, task/page scope, purpose,
  MFA, classification tag grant, unavailable source/application, or incompatible deployment context.
- **No guides**: review installed version and application documentation coverage; publish sources
  or synchronize bundled guides. An application without supplied docs cannot be inherited.
- **Update needs review**: edit the existing page, select a source for the installed version, retain
  or revise the local extension, and save. Concurrent-edit rejection requires reload.
- **Missing context**: select an authorized environment in the reader or use an environment-bound
  application credential. A guide's explicit environment/deployment takes precedence.

Use [Identity and security](security.md), [Application integrations](application-integrations.md),
and [App Factory](app-factory.md) for the corresponding setup and live authorization contracts.
