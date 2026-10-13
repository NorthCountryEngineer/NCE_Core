# AWS application archetypes

The two templates are complete starter repositories. Copy **the contents** of
`templates/static/` or `templates/serverless/` into a new GitHub repository,
commit them, and approve that exact repository in Core's deployment platform
settings. Select its branch in the application deployment form. Core pins a
public branch to its current full commit; private repositories require an exact
commit override.
The static starter has Core sign-in. The serverless starter also has a private
note demonstrating independent `app-read` and `app-write` permissions.

## Declarative repository contract

Every repository needs a root `nce-deploy.json`:

```json
{
  "schema": 1,
  "archetype": "amplify-serverless",
  "frontend": {"directory": ".", "output": "dist"},
  "backend": {"directory": "api", "handler": "handler.handler"}
}
```

Use `amplify-static` and omit `backend` for a frontend-only app. Frontends need
`package.json`, a synchronized `package-lock.json`, and `npm run build` producing
`index.html` in the declared output directory. Paths must remain inside the
checkout. The serverless recipe supports Python 3.12 on ARM64: source is zipped,
never executed during deployment. It uses Lambda's supplied boto3 SDK. Additional
Python dependencies must already be vendored inside the source directory; native
binaries and arbitrary build/deploy commands are not supported by this recipe.
The starter normalizes the documented HTTP API Lambda proxy event formats 1.0
and 2.0; custom handlers must support the QuickCreate integration's event shape.
There is no Fargate recipe yet. A future recipe needs an isolated image builder
and an explicit migration-before-traffic procedure before it can be enabled.

The privileged runner rejects extra manifest fields, path escapes, symbolic
links in Lambda code, and infrastructure changes that remove or replace any
resource. Infrastructure is the reviewed Core template, not a template supplied
by an application. An app cannot request broader IAM permissions through its
manifest. Database tables and media are retained. Retained resources continue
incurring their normal AWS costs until deliberately cleaned up in AWS.
DynamoDB uses its default AWS owned key for encryption at rest, avoiding the
extra KMS charges and account-key permissions of an AWS managed key.

## Release phases and security boundary

1. **Provision:** CodeBuild checks out the trusted Core runner at the platform's
   pinned SHA and the application at the approved SHA. The runner packages Lambda
   code without executing it and creates a manual Amplify app and branch plus an
   optional HTTP API, Lambda, two on-demand DynamoDB tables, and a private media
   bucket. Each Lambda has an individual role with an onboarding permissions
   boundary and access only to that app's resources and Core credential parameter.
2. Core uses the returned origin to register the app's browser and API identity.
   The API credential is written directly to SSM SecureString by Core; it never
   appears in source-builder environment variables, provider state, or HTML.
   Before this configuration, protected API routes fail closed.
3. **Release:** The runner updates the backend's Core identifiers and allowed
   origin. A separate CodeBuild project builds the exact application SHA using
   public configuration only. Its role can write frontend ZIPs and build logs;
   it cannot read Core credentials, assume deployment roles, or change AWS
   infrastructure. The trusted runner then uploads that ZIP to manual Amplify
   Hosting and waits for publication. This publishes the approved commit, even
   when its GitHub branch moves while the build runs.

The trusted runner uses CodeBuild Small and the isolated source builder uses
Medium. They need one available concurrent slot in each compute type. A default
account can allow only one concurrent Small build; using Small for both would
make the waiting runner block its own source build. These jobs are billed while
running, including time the trusted runner waits for its child job. This first
recipe trades a small amount of deployment compute for credential isolation;
it does not change the application's serverless runtime. Core does not request
quota increases automatically.

The provision and release jobs are separately recorded, but both are covered by
the reviewed application plan. No deployment runs until the operator confirms
the billable AWS plan. Platform verification only reads AWS metadata. The adapter
uses short STS sessions, a platform-specific external ID, and an account check.
Verification checks project separation and source authorization; it does not
claim to prove arbitrary external IAM policies are safe. The reviewed onboarding
template is the required permission boundary.

Public frontend variables are `VITE_CORE_ISSUER`,
`VITE_CORE_BROWSER_CLIENT_ID`, `VITE_CORE_ORGANIZATION_ID`,
`VITE_CORE_SERVICE_ID`, `VITE_APPLICATION_ORIGIN`, and `VITE_API_URL`.
Secret-like variable names are rejected. The serverless credential reference is
`/{platform-prefix}/apps/{application-identifier}/core-resource-client`.
SSM uses Standard SecureString parameters and the account's default AWS-managed
Parameter Store key. Custom KMS keys
need a separately reviewed onboarding extension before use.

Core records only a small, allowlisted deployment receipt. It does not copy raw
build logs, CloudFormation outputs, SDK errors, or presigned upload links into
the application console. A stopped or failed runner does not delete provisioned
resources. Stopping a parent runner may leave a source build or publication job
finishing; review AWS when recovering a cancelled deployment.

## Validate the starter without AWS or costs

```sh
cd deployment/aws-apps/templates/serverless
npm ci --no-audit --no-fund
npm run build
python3 -m http.server 8124 --directory dist --bind 127.0.0.1
```

Open `http://127.0.0.1:8124`. Without a configured issuer, sign-in reports that
Core is not configured. This checks the local compiled frontend without touching
AWS. A real deployment uses the public HTTPS origin generated by Core. The API
requires the Core administrator to grant `app-read` or `app-write` and the target
environment boundary explicitly; signing in alone does not grant them.

This creates new Core-managed apps. It does not adopt, rename, or migrate the
existing Axolotl stack or DNS. Existing apps can supply a compatible manifest
after their permission and configuration contract has been reviewed.

Official references: [CodeBuild start_build](https://docs.aws.amazon.com/boto3/latest/reference/services/codebuild/client/start_build.html),
[Amplify manual create_deployment](https://docs.aws.amazon.com/boto3/latest/reference/services/amplify/client/create_deployment.html),
[CloudFormation change sets](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-updating-stacks-changesets.html).
See also [DynamoDB encryption settings](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-dynamodb-table-ssespecification.html).
