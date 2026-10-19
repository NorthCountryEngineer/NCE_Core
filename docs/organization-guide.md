# Welcome to {{platform_name}}

This workspace belongs to **{{organization_name}}**. Your organization manages its own
applications, users, agents, integrations, and data policies through this platform.
Contact **{{support_email}}** for local account and access questions.

## Sign in and find your applications

Use your organization's configured sign-in option. The platform supports its own accounts
and organization-managed cloud or Keycloak identity providers through OIDC/OAuth 2.0.
Choose the organization you intend to work in; belonging to one organization grants no access
to another. Application access and documentation access are assigned through service roles.
If you cannot see an application or document, ask your administrator for the appropriate role.

## Use this documentation library

The organization library combines platform guidance, documentation for your installed
application versions, and local instructions supplied by your administrators. Each page
identifies its source, version, organization revision, and applicable environment/deployment.
Your local guidance may supplement or replace publisher instructions. Read the displayed
provenance and select the intended environment before following a deployment procedure.

Search, page downloads, and AI exports apply the same live access rules.
Administrator guides require a separate documentation grant; being able to read an instruction
does not authorize the operation it describes. Changes to your membership, roles, tags, application
placement, or documentation visibility can remove access immediately.

## Work safely across environments

Your current documentation environment is **{{environment_name}}**. Your namespace is
**{{namespace}}**. Sandbox, development, UAT, and production are separate permission boundaries.
Use synthetic data in training and development exercises. Select the correct application
deployment before copying endpoint or configuration guidance. Never paste passwords,
access tokens, private keys, or sensitive customer records into documentation or AI prompts.

## Chat, AI, and data

Organization administrators define employee profiles and manager assignments in
**Users and permissions → People**. A manager may also report to another manager.
Reporting relationships are separate from application permissions; assigned managers
do not automatically gain access to employees' private application records. See
[Employees and reporting relationships](employee-directory.md) for account setup
and future HR integration boundaries.

Applications can consume centralized chat, approved agent workflows, synthetic training,
and selected data-fabric agreements. These features need explicit application and agent grants.
Federated data sharing requires approval from administrators of both platform deployments.
AI agents have their own accounts and task permissions; they do not borrow your user identity.

AI may retrieve authorized documentation as reference material. A document cannot grant
permissions, approve a release, authorize spending, change a tool registry, or override an
organization's policy. Verify citations and versions when using an AI-generated answer.

## Troubleshooting and support

For a missing document, check organization selection, installed application version, environment,
and documentation role with your administrator. For a denied operation, retain the request ID
and time without copying tokens or record contents. Your administrator can inspect the central
audit trail and deployed-instance health. Documentation may remain downloadable outside the
platform after export; follow your organization's distribution and retention rules.
