# Deploy applications through Core

## Transfer an existing hosted application to another organization

An operator adoption can move an entire existing application to another Core
organization while retaining the existing hosting platform. This is distinct
from installing a new application or changing a shared platform's ownership.
The operator reviews an exact adoption plan before applying its database changes.
Ordinary application forms cannot change organization, hosting platform,
environment, application identifier, service approach, or public origin.

The adoption records an immutable `CloudHostingAssignment` for the exact
application, provider organization/platform/environment, receiving
organization/environment, repository, service approach, identifier and origin.
Both environments must have the same environment name. The provider retains
its deployment platform, credential vault, approved resource references and
other applications. The receiving organization can read its application and
completed release history, but this assignment does not grant access to the
provider's settings, credentials, repositories or other applications.
Deployment changes still require an active human administrator authorized in
both organizations, or a platform administrator, with the provider's live policy
checks. Hosting assignments are checked again at each release step; revocation
or changed placement prevents a subsequent external operation.

Physical AWS names remain bound to the original platform prefix, application
UUID and environment name. For example, the resource prefix can retain a
previous administrative label after ownership changes. Preserve those values,
existing object/database namespaces, OAuth client/service/deployment IDs and
the application's SSM credential path. Changing the organization slug does not
rename AWS resources. No DNS or public-origin change is part of an adoption.
The operator verifies the full application ownership graph and corresponding
runtime tenant mapping before reopening access. Historical release snapshots
and existing audit events retain their original provenance; new adoption
events record the transfer in both organizations.

Use the [operator adoption procedure](application-adoption.md) to review the
exact plan, dependency checks, token revocation and application-side cutover.

Core now has **Deployment platforms** in organization settings and an
**Applications** console. Configure the destination once, verify it, and select
only that destination's approved repository, service approach, and environment
when creating an app. A release records the exact Git commit and a reviewed plan;
its progress and errors remain visible in Core.

The AWS recipes include **Website · Amplify Hosting**, **Website and
serverless API · Amplify + Lambda**, and the fixed **Website, API and workers ·
Amplify + Fargate** recipe described in [Container application releases](cloud-fargate.md).
The first two provision manual Amplify Hosting and,
for the second recipe, a Python Lambda HTTP API, two on-demand DynamoDB tables,
and a private media bucket. Core connects the app's central identity and injects
public browser configuration. Existing users and RBAC remain in Core. The Fargate
recipe keeps frontend source builds isolated and uses approved image digests and
secret/certificate references. Importing the existing Axolotl stack and automated
Cloudflare DNS changes are outside these recipes.
The existing production Axolotl site is not changed by this local exercise.

Production release preparation, approval and every billable build start require
passing GitHub Actions push checks for both the exact application commit and the
pinned Core runner commit. The policies live in Core source and cannot be changed
by `nce-deploy.json`. See [exact-commit CI admission](cicd.md#exact-commit-deployment-admission)
for the checked workflow identities and failure behavior. A pull-request result,
local test receipt, old successful run, or same-named third-party status is not a
substitute for the exact pushed source result.

Game Studio also has a narrowly approved owner-dispatch evidence policy for a
repeatedly complete empty push listing. It remains restricted to the fixed
repository, owner, main branch, reviewed workflow bytes, post-approval run and
required job/check identities. A failed or ambiguous push cannot use it. Adoption
does not change or bypass either CI policy; the exact application and pinned
Core runner revisions still require their approved evidence. See the
[approved Game Studio CI policy](proposals/game-studio-owner-dispatch-ci.md).

A separate, time-limited [one-release Core broker policy](proposals/game-studio-one-release-broker.md)
can attest only its fixed Game Studio source during the approved window. It
retains exact workflow, owner, application and Core source checks, including the
normal Core checks. An adoption neither extends that expiry nor changes its
source pins, native-evidence eligibility or request allowance. Proposal Studio
and other application releases retain their existing policies.

Proposal Studio is private. Before its first CI-gated release, enter a separate
fine-grained GitHub credential under **Deployment platforms → Edit → GitHub CI
read credential**. Limit repository access to the needed source repository and
permissions to **Actions: read** / **Metadata: read**. Keep the token out of chat,
source and application configuration. Core stores it encrypted in its existing
database and does not send it to AWS. Blank input keeps the current credential;
replacement and explicit removal invalidate platform verification and plans.
Re-verify the platform, pin a Core runner commit whose own CI has passed, then
prepare a fresh release. Missing or expired private-repository access blocks
before an AWS build can be started and does not create an automatic retry loop.

## First validate locally without AWS calls or charges

Use this isolated checkout under your home directory. It has its own local
SQLite database and generated development keys. Nothing in this exercise reads
production secrets, creates AWS resources, or deploys the existing Axolotl site.
The simulation uses repository metadata; it does not download or execute app
code. Stop at any error and keep the failed command and its output for diagnosis.

### 1. Open Terminal and clone the branch

```sh
cd "$HOME"
git clone --branch codex/aws-deployment-console \
  https://github.com/NorthCountryEngineer/NCE_Core.git \
  nce-deployment-preview
cd "$HOME/nce-deployment-preview"
```

If that preview directory already exists, preserve it and use a new directory
name in every command below. Do not reset or remove your normal Core checkout.

### 2. Select local configuration and install the locked local dependencies

```sh
unset DATABASE_URL CORE_ISSUER CORE_ALLOWED_HOSTS CORE_TRUST_PROXY
unset CORE_DEBUG DJANGO_SETTINGS_MODULE CORE_OIDC_PREVIOUS_KEY_FILES
unset CORE_SECRET_KEY_FILE CORE_OIDC_KEY_FILE
export CORE_ENABLE_CLOUD_DEPLOYMENTS="0"
uv sync --frozen
```

`uv` is already available on your Mac. This uses the repository's isolated
virtual environment; it does not change your system Python. AWS credentials are
not needed. The live AWS gate remains disabled even if your shell has an AWS
profile from earlier deployment work.

### 3. Initialize the isolated database and the demo organization

```sh
uv run python manage.py --local migrate --noinput
uv run python manage.py --local cloud_deployment_demo \
  --organization deployment-demo
```

Expected result: a URL ending in
`/organizations/deployment-demo/platform/cloud/apps/` and a local-only account.
The demo command preserves an existing demo account's password.

### 4. Start Core and keep this terminal open

```sh
uv run python manage.py dev
```

Open <http://localhost:8000/accounts/login/> and sign in using:

- Email: `deployment-demo@example.test`
- Initial local demo password: `Local-demo-only-2026!`

This account is synthetic and for this isolated local database only.

If port 8000 is busy, stop only the known preview server, or use
`uv run python manage.py dev --port 8010` and open `http://localhost:8010` in all
browser steps. Do not stop an unfamiliar process.

### 5. Start the background worker in a second terminal

```sh
cd "$HOME/nce-deployment-preview"
unset DATABASE_URL CORE_ISSUER CORE_ALLOWED_HOSTS CORE_TRUST_PROXY
unset CORE_DEBUG DJANGO_SETTINGS_MODULE CORE_OIDC_PREVIOUS_KEY_FILES
unset CORE_SECRET_KEY_FILE CORE_OIDC_KEY_FILE
export CORE_ENABLE_CLOUD_DEPLOYMENTS="0"
uv run python manage.py --local core_worker
```

Keep both terminals open. The worker advances the simulation while you use the
console. If you used port 8010, set `CORE_ISSUER=http://localhost:8010` in this
worker terminal before starting it.

### 6. Inspect the configured destination

Open <http://localhost:8000/organizations/deployment-demo/platform/cloud/platforms/>.
Choose **AWS preview · simulation**. It should be verified and available, with
**Development** as its environment and `NorthCountryEngineer/Axolotl_News` as an
approved repository. Simulation settings have no AWS account credentials.

To try settings yourself, choose **Add platform**, name it `Second preview`,
choose **Simulation · no AWS calls or charges**, enter resource prefix
`preview-two`, and enter `NorthCountryEngineer/Axolotl_News` in **Approved GitHub
repositories**. Select Development and both application templates, save, then
choose **Verify platform**. An unverified or disabled platform is not offered
when creating an app. Saving changed settings requires verification again.

### 7. Create an application

Open <http://localhost:8000/organizations/deployment-demo/platform/cloud/apps/>
and choose **Create application**. Enter:

| Field | Value |
| --- | --- |
| Application name | Axolotl deployment preview |
| Application identifier | axolotl-preview |
| Deployment platform | AWS preview · simulation |
| GitHub repository | NorthCountryEngineer/Axolotl_News |
| Branch | main |
| Commit override (optional) | Leave blank |
| Service approach | Website and serverless API · Amplify + Lambda |
| Environment | Development |
| Application URL | Leave blank |

Simulation pins a sample revision without contacting GitHub. It is not a
production release recommendation. Live deployment must use a reviewed commit
with the supported
`nce-deploy.json` contract. Select **Create application**.

### 8. Review and start the simulated deployment

Choose **Prepare release**. Review the repository, exact revision, destination,
environment, and resource plan. Check **Deploy this exact reviewed release** and
choose **Start simulated deployment**. There is no AWS charge checkbox in
simulation mode because it does not contact AWS.

Wait about 10–15 seconds with the worker running, then choose **Refresh progress**.
Expected result: completed simulated stages and clearly labeled simulated output.
This is evidence that Core's deployment lifecycle works locally, not that any
cloud resource exists. The mock application address will not serve an actual app.

### 9. Check update and isolation behavior

1. Return to the app and choose **Edit application settings and source revision**.
   Change **Commit override (optional)** to a valid 40-character fixture such as
   `1111111111111111111111111111111111111111`, save, and prepare a new release.
   The previous release keeps its original revision and plan.
2. Inspect the deployment platform settings. An unapproved repository is absent
   from the application's repository choices. Environments and service approaches
   appear only when that platform permits them.
3. Change the platform's settings and save. It becomes unverified until verified
   again; a release reviewed against its old configuration cannot deploy.
4. Stop the worker with Ctrl-C. A newly approved simulated release stays queued.
   Restart the worker and refresh to see it advance. Stop both preview terminals
   with Ctrl-C when finished.

### 10. Run the automated local checks

```sh
cd "$HOME/nce-deployment-preview"
export CORE_ENABLE_CLOUD_DEPLOYMENTS="0"
uv run python manage.py --local test \
  platform_services.tests.test_cloud_deployments \
  platform_services.tests.test_cloud_portal \
  platform_services.tests.test_cloud_aws \
  platform_services.tests.test_cloud_recovery \
  platform_services.tests.test_cloud_release_config --noinput
uv run python manage.py --local check
python3 deployment/aws-platform/check_template.py
```

The tests use isolated test databases and stub AWS responses. They do not start
CodeBuild, Amplify builds, or any paid infrastructure.

## One-time live AWS onboarding

Do this section only when you choose to authorize real AWS resources and their
charges. The application creation UI remains usable in simulation without it.
Onboarding needs an administrator who can create the named roles and projects.
The resulting runtime permissions are narrow; Core does not need administrator
credentials. Do not enter access keys or GitHub personal access tokens in Core.

### 11. Review the sources and AWS account

Review `deployment/aws-platform/platform.template.json`,
`deployment/aws-apps/application.template.json`, and the exact Core runner commit.
The trusted runner provisions and publishes; the isolated source builder may
execute application install/build code but has no deployment or application runtime secret access.
Stateful application resources are retained. Disabling a platform does not stop
or delete an existing application.

```sh
cd "$HOME/nce-deployment-preview"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
export AWS_PROFILE="axolotl-deploy"
export AWS_REGION="us-east-1"
export AWS_DEFAULT_REGION="us-east-1"
export AWS_PAGER=""
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
aws sts get-caller-identity --profile axolotl-deploy --region us-east-1
```

The account must be `015454941623`. If it is different, stop. If the profile is
expired, run `aws login --profile axolotl-deploy`, then check again. That profile
uses AWS login; `aws sso login` is not the matching command unless you separately
configured an IAM Identity Center profile.

### 12. Connect only the approved GitHub repositories

First create a compatible starter repository. These commands publish source to
GitHub without starting an AWS build. If the chosen folder or repository already
exists, stop and choose a new name rather than replacing it:

```sh
cd "$HOME/nce-deployment-preview"
mkdir "$HOME/nce-serverless-starter" && \
cp -R deployment/aws-apps/templates/serverless/. "$HOME/nce-serverless-starter/" && \
cd "$HOME/nce-serverless-starter" && \
git init -b main && \
git add . && \
git commit -m "Start Core-connected serverless application" && \
gh repo create NorthCountryEngineer/NCE_Serverless_Starter \
  --public --source=. --remote=origin --push
cd "$HOME/nce-deployment-preview"
export NCE_APPLICATION_REPOSITORY="NorthCountryEngineer/NCE_Serverless_Starter"
```

For a frontend-only starter, use `templates/static/.` instead and give the folder
and GitHub repository different names. You can mark each repository as a template
in **GitHub repository → Settings → General → Template repository**. Future apps
can use GitHub's **Use this template → Create a new repository**. The new app
repository must still be approved in the AWS connection and Core platform settings.

1. Open the [AWS Developer Tools connections console](https://console.aws.amazon.com/codesuite/settings/connections?region=us-east-1).
2. Confirm region **US East (N. Virginia)** and choose **Create connection**.
3. Choose **GitHub**, name it `nce-core-deployments`, and choose **Connect to GitHub**.
4. Authorize the AWS Connector GitHub App. Choose your NorthCountryEngineer account.
5. Choose **Only select repositories** and include `NCE_Core` and the approved
   application repositories, initially `NCE_Serverless_Starter`. Avoid installing access
   to unrelated repositories.
6. Return to AWS, choose the installation, and complete **Connect**.
7. Confirm status **Available**. Copy its `arn:aws:codeconnections:us-east-1:015454941623:connection/...` ARN.

AWS requires this browser authorization. A connection created through CLI or
CloudFormation remains Pending until the handshake is completed. See
[AWS's connection instructions](https://docs.aws.amazon.com/dtconsole/latest/userguide/connections-create-github.html).

### 13. Discover the existing Core runtime role and pin this reviewed runner

In **AWS console → Service Quotas → AWS services → AWS CodeBuild**, confirm the
applied limits for **Concurrently running builds for Linux/Small** and
**Concurrently running builds for Linux/Medium** are each at least **1**, with a
free slot in each category. The runner uses Small and waits for the source build
on Medium. Other builds consuming either category can cause queuing. Stop here
if the account cannot run both; do not raise quotas automatically. Both running
projects accrue CodeBuild charges. See [AWS's quota documentation](https://docs.aws.amazon.com/codebuild/latest/userguide/limits.html).

```sh
export NCE_PLATFORM_STACK="nce-family-deployment-platform"
export NCE_PLATFORM_PREFIX="nce-family"
export NCE_RUNNER_COMMIT="$(git rev-parse HEAD)"
export NCE_CORE_EXTERNAL_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
export NCE_CORE_TASK_DEFINITION="$(aws cloudformation describe-stacks \
  --stack-name nce-core-production \
  --query 'Stacks[0].Outputs[?OutputKey==`TaskDefinition`].OutputValue | [0]' \
  --output text --profile axolotl-deploy --region us-east-1)"
export NCE_CORE_RUNTIME_ROLE_ARN="$(aws ecs describe-task-definition \
  --task-definition "$NCE_CORE_TASK_DEFINITION" \
  --query 'taskDefinition.taskRoleArn' --output text \
  --profile axolotl-deploy --region us-east-1)"
export NCE_CORE_RUNTIME_ROLE_NAME="${NCE_CORE_RUNTIME_ROLE_ARN##*/}"
printf 'Core runtime role: %s\nRunner commit: %s\n' \
  "$NCE_CORE_RUNTIME_ROLE_ARN" "$NCE_RUNNER_COMMIT"
```

Use a unique platform prefix per organization. This template's role and data
permissions use that prefix; sharing it between organizations would weaken their
AWS isolation. Do not supply the ECS task execution role or the account root ARN.
Core rejects reuse of an AWS account/region/prefix or control role in a second
platform registration. Approve a new role and prefix for a separate destination.

Paste your connection ARN in this command, replacing the example value:

```sh
export NCE_CODE_CONNECTION_ARN="PASTE_THE_AVAILABLE_CONNECTION_ARN_HERE"
```

### 14. Validate and create the one-time platform stack

These commands create AWS resources. Storage and later builds may incur charges.
The stack starts no builds automatically.

```sh
aws cloudformation validate-template \
  --template-body file://deployment/aws-platform/platform.template.json \
  --profile axolotl-deploy --region us-east-1

aws cloudformation deploy \
  --stack-name "$NCE_PLATFORM_STACK" \
  --template-file deployment/aws-platform/platform.template.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    "Prefix=$NCE_PLATFORM_PREFIX" \
    "CoreRuntimeRoleArn=$NCE_CORE_RUNTIME_ROLE_ARN" \
    "CoreRuntimeRoleName=$NCE_CORE_RUNTIME_ROLE_NAME" \
    "CoreExternalId=$NCE_CORE_EXTERNAL_ID" \
    "CodeConnectionArn=$NCE_CODE_CONNECTION_ARN" \
    "RunnerCommit=$NCE_RUNNER_COMMIT" \
    "RunnerBranch=codex/aws-deployment-console" \
    "ApplicationRepositories=$NCE_APPLICATION_REPOSITORY" \
    "DefaultApplicationRepository=$NCE_APPLICATION_REPOSITORY" \
    "ApplicationBranches=main" \
  --profile axolotl-deploy --region us-east-1

aws cloudformation describe-stacks \
  --stack-name "$NCE_PLATFORM_STACK" \
  --query 'Stacks[0].Outputs' --output table \
  --profile axolotl-deploy --region us-east-1
```

The stack creates both sides of the STS permission: the control role trusts the
exact Core runtime role plus external ID, and the runtime role gets permission
to assume only that control role. The source build project has no IAM pass-role,
SSM, CloudFormation, or Amplify access. The deployment project can pass only the
approved CloudFormation role. Per-app Lambda roles must use the boundary and
receive only their own app's data permissions.

`validate-template` checks CloudFormation syntax; it does not prove all IAM
conditions or deployment operations in this account. If available, run your
approved pinned `cfn-lint` and `cfn-guard` checks before creating the stack. Those
tools were not installed or invoked on this laptop by this feature work.

### 15. Install this Core branch in your existing AWS Core deployment

Use the existing [AWS Core release instructions](../infra/aws/README.md) for the
new Core image. This branch adds additive database migrations and the
`EnableCloudDeployments` stack parameter. Apply the migration before the new web
and worker serve requests, as in the branded-auth deployment recovery. Use the
new `infra/aws/core-fargate.template.json` rather than `--use-previous-template`
when first adding this parameter.

Set `EnableCloudDeployments=1` only after the destination is onboarded. It enables
live AWS calls in both the Core web service and background worker. Its default
is `0`. This does not itself authorize a release; each live release still requires
its own explicit cost confirmation in the UI. Keep the same issuer, database,
keys, and Core runtime secret.

The commands below upgrade the existing Core backend. They incur AWS charges
when you run them and briefly stop Core sign-in while migrations run. Core's
existing Amplify proxy needs no rebuild. Execute every block in the same terminal;
stop at any error.

Open Docker Desktop, then paste:

```sh
cd "$HOME/nce-deployment-preview"
export CORE_STACK="nce-core-production"
export CORE_REGISTRY_STACK="nce-core-production-registry"
export NCE_RELEASE_DIR="$HOME/.axolotl-aws/cloud-console-release"
mkdir -p "$NCE_RELEASE_DIR"
docker info >/dev/null
export NCE_IMAGE_TAG="cloud-console-$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%d%H%M%S)"
export CORE_REPOSITORY_URI="$(aws cloudformation describe-stacks \
  --stack-name "$CORE_REGISTRY_STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`RepositoryUri`].OutputValue | [0]' \
  --output text --profile axolotl-deploy --region us-east-1)"
export CORE_REPOSITORY_NAME="${CORE_REPOSITORY_URI#*/}"
export CORE_REGISTRY_HOST="${CORE_REPOSITORY_URI%%/*}"
printf 'Repository: %s\nImage tag: %s\n' "$CORE_REPOSITORY_URI" "$NCE_IMAGE_TAG"
```

The repository must begin `015454941623.dkr.ecr.us-east-1.amazonaws.com/`.
If it is empty or `None`, stop. Require passing push CI for this exact source
commit before publishing, then build, publish, and record the digest:

```sh
python3 ci/github_gate.py --repository NorthCountryEngineer/NCE_Core \
  --commit "$(git rev-parse HEAD)"
docker build --platform linux/arm64 -f deployment/Dockerfile.aws \
  -t "$CORE_REPOSITORY_URI:$NCE_IMAGE_TAG" .
aws ecr get-login-password --profile axolotl-deploy --region us-east-1 \
  | docker login --username AWS --password-stdin "$CORE_REGISTRY_HOST"
docker push "$CORE_REPOSITORY_URI:$NCE_IMAGE_TAG"
export NCE_IMAGE_DIGEST="$(aws ecr describe-images \
  --repository-name "$CORE_REPOSITORY_NAME" --image-ids "imageTag=$NCE_IMAGE_TAG" \
  --query 'imageDetails[0].imageDigest' --output text \
  --profile axolotl-deploy --region us-east-1)"
export NCE_IMAGE_URI="$CORE_REPOSITORY_URI@$NCE_IMAGE_DIGEST"
aws cloudformation describe-stacks --stack-name "$CORE_STACK" \
  --output json --profile axolotl-deploy --region us-east-1 \
  > "$NCE_RELEASE_DIR/core-stack-before.json"
python3 infra/aws/prepare-cloud-console-release.py \
  --stack-file "$NCE_RELEASE_DIR/core-stack-before.json" \
  --image "$NCE_IMAGE_URI" --commit "$(git rev-parse HEAD)" --enable-live 1 \
  --output-directory "$NCE_RELEASE_DIR"
```

The helper checks the account, stack state, digest, and passing GitHub Actions
checks for the exact Core source commit. It prepares updates locally, preserves
every other stack parameter, and adds the new operator switch. The private
`core-ci.json` receipt binds the checked source SHA to the reviewed image digest.
Do not use a receipt for a different image or source revision.
Use the new template for the first update so CloudFormation knows that parameter:

```sh
aws cloudformation update-stack --stack-name "$CORE_STACK" \
  --template-body file://infra/aws/core-fargate.template.json \
  --capabilities CAPABILITY_IAM \
  --parameters "file://$NCE_RELEASE_DIR/core-cloud-stopped.json" \
  --profile axolotl-deploy --region us-east-1
aws cloudformation wait stack-update-complete --stack-name "$CORE_STACK" \
  --profile axolotl-deploy --region us-east-1
CORE_ENABLE_AWS_DEPLOYMENT=true uv run python infra/aws/run-management.py \
  --stack "$CORE_STACK" --region us-east-1 -- migrate --noinput
```

Wait for **Release command completed successfully**. The new cloud deployment
migrations should report `platform_services.0035... OK` and
`platform_services.0036... OK` and `platform_services.0038... OK`, or already applied on a retry. If migration fails,
keep Core stopped and inspect its named CloudWatch
web log stream. Then start the new service:

```sh
aws cloudformation update-stack --stack-name "$CORE_STACK" \
  --use-previous-template --capabilities CAPABILITY_IAM \
  --parameters "file://$NCE_RELEASE_DIR/core-cloud-running.json" \
  --profile axolotl-deploy --region us-east-1
aws cloudformation wait stack-update-complete --stack-name "$CORE_STACK" \
  --profile axolotl-deploy --region us-east-1
export CORE_CLUSTER="$(aws cloudformation describe-stacks --stack-name "$CORE_STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`Cluster`].OutputValue | [0]' \
  --output text --profile axolotl-deploy --region us-east-1)"
export CORE_SERVICE="$(aws cloudformation describe-stacks --stack-name "$CORE_STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`Service`].OutputValue | [0]' \
  --output text --profile axolotl-deploy --region us-east-1)"
aws ecs wait services-stable --cluster "$CORE_CLUSTER" --services "$CORE_SERVICE" \
  --profile axolotl-deploy --region us-east-1
```

Open <https://core.nnytech.ai/accounts/login/>. Sign in with
your existing account and confirm the deployment console opens before proceeding.
If you need to restore the old image, run the same `update-stack` command using
`core-cloud-recovery.json`, then its waiter. The recovery disables live deployment
and preserves the additive database schema. Do not regenerate keys or bootstrap
users. Service stability alone does not prove the public page is healthy.

### 16. Enter the platform settings once

Sign into your deployed Core administrator account. Open:

`https://core.nnytech.ai/organizations/family/platform/cloud/platforms/`

Choose **Add platform** and enter:

| Core field | Value |
| --- | --- |
| Platform name | AWS · family production |
| Deployment mode | AWS · real deployments |
| AWS account ID | `015454941623` |
| AWS region | `us-east-1` |
| Deployment role ARN | Stack output `ControlRoleArn` |
| Role external ID | Stack output `CoreExternalId` |
| CodeBuild deployment runner | Stack output `RunnerProjectName` |
| GitHub CodeConnection ARN | Stack output `CodeConnectionArn` |
| Isolated source build project | Stack output `BuildProjectName` |
| Deployment artifact bucket | Stack output `ArtifactBucket` |
| CloudFormation execution role ARN | Stack output `CloudFormationExecutionRoleArn` |
| Application role permissions boundary ARN | Stack output `AppRoleBoundaryArn` |
| Approved deployment runner repository | `NorthCountryEngineer/NCE_Core` |
| Approved runner commit | Stack output `RunnerCommit` |
| AWS resource prefix | Stack output `Prefix` |
| Approved GitHub repositories | `NorthCountryEngineer/NCE_Serverless_Starter` |
| Available environments | Only the Core environments this destination should deploy |
| Available application templates | The two implemented AWS recipes |

Choose **Save platform settings**, then **Verify platform**. Verification checks
configured resources and access without starting a build or creating an app.
Only verified active platforms are available in the app creation UI. Changed
settings invalidate verification and any release plan that depended on them.

Do not use this local simulation account on the Internet. Use your actual Core
account with organization administrator access for the live console.

## Deploy an app after onboarding

### 17. Start from a compatible GitHub repository

The starter archetypes live in `deployment/aws-apps/templates/` in this branch. Put the
selected starter into an app repository and keep its `nce-deploy.json` at the
repository root. The manifest describes the frontend directory/output and the
optional Python backend directory/handler. The privileged runner accepts only
this known contract and the built-in recipe; a repository cannot select IAM
permissions, secrets, a different AWS account, or arbitrary deployment commands.

The first version does not generate a new GitHub repository from Core. Create it
with GitHub's normal template/repository UI, or use an existing compatible repo.
Onboard it once by adding it to the CodeConnections installation, the platform
stack's repository/branch allowlist, and Core's approved repositories, then verify
the platform again. Future releases of approved repositories use the console.
The existing Axolotl repository needs a compatible manifest before live use; do
not import the existing site into this recipe as a new app without reviewing the
new resources and identity registration.

### 18. Create, review, and deploy in Core

1. Choose **Applications → Create application**.
2. Choose the configured AWS platform, an approved repository, and its branch.
   Leave **Commit override (optional)** blank to resolve a public repository's
   branch to its current revision. For a private repository, paste its full
   40-character Git commit. Review shows the pinned revision, which is used by
   both the source build and backend package.
3. Choose a supported service approach and one of that platform's environments.
4. Leave **Application URL** blank for the generated Amplify address. To use a
   custom origin, first confirm its existing hosting/domain setup and enter its
   HTTPS origin without a path.
5. Save and choose **Prepare release**. Review the infrastructure, authentication
   setup, exact revision, destination, and expected charge categories.
6. Check **Deploy this exact reviewed release** and **I authorize AWS resources and
   deployment charges for this release**, then choose **Deploy release**.
7. Follow **Refresh progress** until provisioning, Core identity configuration,
   isolated source build, backend release, and frontend publication complete.
8. Open the resulting application address. Test sign-in through the app-branded
   Core page and grant ordinary users the app's appropriate RBAC roles through
   Core. Account creation does not imply publishing access.

The frontend ZIP is produced in a separate source build and deployed manually to
Amplify; the trusted runner never executes application build commands. Backend
code is packaged from the same reviewed commit. Core records statuses and
sanitized errors. Source credentials and resource-client secrets do not appear
in the browser or deployment event output.

## Costs, failures, and limits

Live deployment can charge for CodeBuild minutes, Amplify hosting/traffic, Lambda
requests/runtime, API Gateway requests, DynamoDB reads/writes/storage, S3
storage/requests/transfer, and CloudWatch logs. Review the expected traffic and
storage in the [AWS Pricing Calculator](https://calculator.aws/) before authorizing
live deployment. The current console does not calculate or enforce an exact
monthly AWS bill. No paid AI provider is involved in these recipes.

A failed release stays visible. Review its failed stage before retrying. A
provider failure may leave resources created by an earlier successful stage;
Core does not delete app data or recreate production infrastructure merely to
clear an error. Keep resource/plan identity stable and review AWS stack events
for a failed CloudFormation stage. Disable the platform to prevent new Core
runs; use AWS's reviewed resource-specific cleanup procedures to stop charges.

If a build's start response is lost, Core searches the runner project using the
saved run token rather than starting a second billable build. When that outcome
remains uncertain, the release fails safely and shows a **Recovery token**. In
**CodeBuild → the platform's runner project → Build history**, inspect each recent
run's `NCE_RUN_TOKEN` environment variable. Confirm its status and stop an active
or queued runner before approving another release. A **Build id** output names a
confirmed run directly. Stopping a runner does not undo CloudFormation operations,
already published artifacts, or application data; a separately launched source
build may also need cancellation in its source project.

Other deployment providers can use the same settings and reviewed release
lifecycle after their adapter and security onboarding are implemented. This
version offers only AWS static and serverless recipes, not an arbitrary hosting
or container deployment claim.


## Academy container recipe

The `amplify-academy-fargate` recipe is a separate fixed trusted recipe for
NCAcademy. It uses the reviewed container network, HTTPS edge and retained private
PostgreSQL structure with one API and a migration task. It provisions no browser,
worker, object bucket or application task role. Application source cannot select
commands, ports, roles, templates or secret values. Its root manifest declares
`backend.image` as an immutable approved private ECR digest, frontend directory
and output, and the `/api/auth/core/callback` identity contract.

Operator-owned platform `academy_fargate_apps` maps each approved repository to
`image_repository`, `runtime_secret_arn`, `certificate_arn` and `api_host`.
Optional boolean `allow_live_models` defaults to false and controls the runtime
provider gate. Application manifests cannot enable live model processing. The
existing Fargate CloudFormation role/permission boundary is reused by this recipe;
separate application and migration execution roles can pull only the approved API
repository, inject their assigned deployment secrets and Core parameter, and write
application logs. Their names remain inside the existing pass-role and secret
namespaces; no platform policy expansion is required.

The retained `DatabaseSecret` owns PostgreSQL and is injected only into the
one-shot migration. The separate retained `DatabaseRuntimeSecret` holds the
`academy_runtime` login injected into the API. Migration receives that target
login as `ACADEMY_RUNTIME_DB_USERNAME` and `ACADEMY_RUNTIME_DB_PASSWORD`, creates
or rotates it after schema preparation, and grants only the application's table
operations. API access to audit events is SELECT/INSERT only; schema ownership
and audit UPDATE/DELETE remain unavailable. The API execution role cannot read
the database owner secret. The migration task definition selects its dedicated
execution role; neither task receives an application AWS task role.

The ARM64 API command is `python -m academy.runtime api`, migration is
`python -m academy.migrate`, and readiness is `/api/ready` on port 8000. Core first
provisions zero desired tasks, registers the public PKCE client and confidential
resource client, injects secrets, drains the API, runs migration and only starts
the API after exit code zero. A failed migration leaves the API stopped. Runtime
root filesystems are read-only with nonroot UID 10001 and temporary scratch
volumes. Database connections require verify-full TLS and the RDS trust bundle.

Application permissions are `academy-use`, `academy-teach` and `academy-audit`;
Core's reserved `admin` remains separate. The exact source repository is planned
as `NorthCountryEngineer/NCAcademy`; CI admission requires its exact SHA's successful
push run of `.github/workflows/ci.yml` with `NCAcademy required checks`. A different
repository requires a separate reviewed Core CI policy change.

Operators must review/migrate/test this change, pin a new trusted Core runner
commit and enable the recipe in platform settings before preparing deployment.
Retained database backups, a stable external vault bootstrap, matching API/frontend
images and an independently tested restore are required before production.
An independent retained audit checkpoint destination remains required before
admitting real student records; database append-only grants alone cannot protect
against database administrators.

### Optional Academy family PII key

Local student and parent identities use a dedicated application-layer PII key.
The trusted Academy template accepts the **operator-owned** `PiiSecretArn` stack
parameter, empty by default. Its full Secrets Manager ARN identifies a separate
retained secret in the stack's account and region. The secret's entire
`SecretString` is the canonical padded URL-safe base64 encoding of 32 random
bytes. No value, JSON selector or secret reference is accepted from the
application manifest, frontend variables or platform application configuration.
The PII secret must not be the shared `RuntimeSecretArn` or either database
credential secret. The template explicitly rejects reuse of the shared runtime
secret; the operator reviews the remaining independent secret bindings.

With a nonempty binding, ECS injects `PII_ENCRYPTION_KEY` into the API only. Its
execution role gains a separate exact-ARN `secretsmanager:GetSecretValue` policy
constrained to the stack account and requested region. Migration, archive,
frontend and application task roles receive no PII key or PII secret grant.
This initial contract uses the account's AWS-managed Secrets Manager encryption
key; a customer-managed key requires separate reviewed decrypt/key-policy work.
No family secret is created, rotated, deleted or output by this application
template. Back it up independently and retain it through database restores;
blind replacement would make private data and username indexes unreadable.

Create the retained dedicated secret before installing the binding through a
reviewed CloudFormation change set. Preserve all other stack parameters,
resources and stateful IDs. A first/legacy stack without the parameter defaults
to disabled; existing stacks retain the operator's prior value with
`UsePreviousValue` on both provision and release/activation. The runner validates
the retained ARN's account, region and separation from the shared runtime secret
before creating a change set. Provision continues to preserve the existing API
image and desired count; release stops admission, migrates with the owner
credential, then activates the matching image. The PII key is unnecessary for
the additive schema migration and is never given to it.

This extension changes the trusted recipe and runner only. After exact-source
Core CI, pin their reviewed commit in the platform's Runner/SourceBuilder
projects; the running Core backend image can remain on its verified release
because no backend model, configuration validator, registration, snapshot or
permission contract changes. A future runner must retain this stack binding.
Verify API-only injection, exact scoped role permission, absence from migration
and public artifacts, key backup/restore and synthetic account/login/reset before
admitting real student records. Offline checks cover the template, parameter
preservation, manifest rejection and migration boundary; they do not prove live
secret configuration, retention or restore.

References: [ECS Secrets Manager injection](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html),
[IAM resource account and requested region conditions](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html).


### Optional Academy teacher agents

Academy school permissions include `academy-school` in addition to use, teach
and audit. This is an application capability; it never grants Core's reserved
human-only organization `admin`. Local school/class membership further limits it.

The operator-approved `academy_fargate_apps` repository map accepts optional
`allow_agent_teachers` and `allow_live_models` booleans, independently defaulting
to false. The former maps only to the fixed recipe's `AllowAgentTeachers` flag.
When enabled, ECS injects `CORE_WORKLOAD_TOKEN` from the `core_workload_token` JSON
key of that application's already-approved `RuntimeSecretArn` into its API.
No new ARN, IAM grant, application task role or frontend secret is accepted.
Migration always receives `ALLOW_AGENT_TEACHERS=false` and no workload token.
The existing paired confidential Basic client remains in its SSM JSON parameter.

Before enabling, separately approve the backend workload credential, agent
account/binding, consumer/environment/task, service roles, resource registrations
and workload-compatible tags in Core, plus the exact local school/class placement
in Academy. Missing or denied authority fails closed. Human owner-only policy is
not changed by this template. Enabling agent authentication never enables model
inference, starts a model runner or issues a credential automatically.

## Game Studio application archetype

`amplify-game-studio` is a dedicated reviewed recipe for
`NorthCountryEngineer/Game_Studio`. It deploys an independent tenant/environment
installation: Amplify Hosting, one Python Lambda HTTP API, five retained
on-demand DynamoDB tables (private compatibility context, public compatibility
context, private agent state, private games/settings, and public game editions
and boards), a private versioned media bucket, and bounded logs. This is separate
from Axolotl News; it does not migrate, unpublish, or change existing games there.
No application or model code runs on the Core host or privileged runner.

The source repository must contain:

```json
{
  "schema": 1,
  "archetype": "amplify-game-studio",
  "frontend": {"directory": ".", "output": "dist"},
  "backend": {"directory": "backend/nce_api", "handler": "handler.handler"}
}
```

`backend/nce_api/agent-framework.json` is required and packaged with the Python
modules. Application manifests cannot provide identity, infrastructure, IAM,
custom commands, or a different handler/directory for this archetype. The runner
uses only its pinned `deployment/aws-apps/game-studio.template.json`.

### Configure the platform once

1. Review and install the Core revision containing this archetype and migration
   `0042_game_studio_archetype`. Run the normal Core checks and migrations before
   enabling it in an existing deployment console. This source change alone does
   not deploy resources or change AWS permissions.
2. Update the existing AWS platform onboarding stack to that same immutable Core
   runner commit. Preserve the current account, region, resource prefix, role
   bindings, approved existing repositories, connection and existing services.
   Add `NorthCountryEngineer/Game_Studio` to the approved repository list and the
   existing GitHub CodeConnection installation. The boundary adds only
   `dynamodb:ConditionCheckItem` in the existing table namespace so transactional
   game configuration, draft, edition and score guards can work. Review the
   CloudFormation change set; do not replace the platform or recreate its data.
3. Keep onboarding parameter `GameStudioModelArns` empty for the free guided
   designer. Optional paid model onboarding is described below; no model calls
   or new model resources occur during registration or simulation.
4. In Core, open **Settings → Deployment platforms**, edit the existing verified
   AWS platform, and approve the **Game Studio · Amplify + serverless API**
   template for the intended tenant environments. Use **Approved Game Studio
   integrations** to set an operator-owned repository map, for example:

```json
{
  "NorthCountryEngineer/Game_Studio": {
    "version": 1,
    "branding": {
      "appName": "Game Studio",
      "badgeText": "Powered by Game Studio",
      "accentColor": "#176a63"
    },
    "hosts": [
      {
        "id": "academy",
        "name": "Academy lessons",
        "origin": "https://academy.example.com",
        "returnPath": "/lessons/games"
      }
    ],
    "allow_paid_models": false,
    "bedrock_model_arns": []
  }
}
```

Replace the example host with the application's actual canonical HTTPS origin,
or use `"hosts": []` until integration is configured. Origins must use lowercase
hostnames, omit a default `:443`, have no path, credentials, query or fragment,
and be listed once. Return paths are ordinary same-origin paths without query,
fragment, traversal or an external redirect. Labels are plain text. The public
`version/branding/hosts` projection must fit 2048 UTF-8 bytes to leave room for
other Lambda environment variables; model policy settings do not enter that
public projection. Save and **Verify** the platform again after any change.

### Create and release a tenant installation

1. Open **Applications → Create application** in the intended Core organization.
   Select only its verified AWS platform and configured environment.
2. Choose repository `NorthCountryEngineer/Game_Studio`, the intended branch,
   and **Game Studio · Amplify + serverless API**. Use an exact commit override
   when required. Supply an existing HTTPS custom origin or leave it blank for
   the generated Amplify origin. Core does not create a domain or DNS records.
3. Prepare a release. The repository's exact pushed commit must have a successful
   `.github/workflows/ci.yml` job named **Game Studio required checks**; the
   trusted Core runner commit must independently pass **Core required checks**.
4. Review the plan and its ongoing AWS costs. Only when deployment charges are
   authorized, approve the release. Existing global and per-release cost gates
   remain in force. Simulation runs the identity/orchestration flow without AWS
   calls and does not prove cloud availability.
5. Core provisions the independent backend, registers its browser and private
   resource clients, writes the API credential directly to its own SSM
   SecureString, configures the backend's exact Core binding, then builds and
   publishes the frontend at that same reviewed commit. The isolated builder
   receives only public `VITE_` configuration. The runner supplies the public
   `GAME_STUDIO_CONFIG_JSON` build value through its trusted build entrypoint.
6. On the resulting Game Studio page, sign in through the app-branded Core flow.
   Assign explicit application accounts/roles through Core or Game Studio account
   administration. `player` grants application membership for shared score
   submission without creation or administration; `creator` grants private creation and playtesting;
   `publisher` additionally grants publication; `administrator` additionally
   manages content, creation settings, branding, agents and invitations. These
   roles grant no organization administration or access to other applications.
   Host-application administration does not imply Game Studio creation rights.
7. Create a private game, test every level, mark that exact revision reviewed,
   and publish it. Confirm its public player and optional shared score board.
   Editing a draft leaves the previous public edition intact; republishing
   resets that edition's board. Existing cleanup and optimistic revisions apply.

Game creation and account sign-in always open in a top-level Game Studio page.
Only a published game's `/embed/games/{id}` player is embeddable; private drafts,
creator controls and authentication pages deny framing. The trusted runner
replaces any repository-produced `customHttp.yml` in the final frontend ZIP
with its own exact-host framing policy. Public `/sdk/**` modules intentionally
allow cross-origin loading; they carry no credentials and confer no privileges.
Host lists are changed only through reviewed platform settings and a release.
Branding can subsequently be changed by the installation's `games-manage`
administrator using the versioned integration API. See the Game Studio SDK
contract for launch, publish selection and public player events.

### Optional paid conversational models

The default guided designer and all local tests invoke no paid provider. To
permit Bedrock conversational design/progression, the operator must first add
an explicit list of exact foundation-model/inference-profile ARNs to onboarding
parameter `GameStudioModelArns` and review/apply that boundary update. Then set
`allow_paid_models: true` and `bedrock_model_arns` for this repository in platform
settings, using only a reviewed subset of the admitted ARNs. Wildcards and
foreign-account inference profiles are rejected. Inference profiles can require
both the profile ARN and underlying regional foundation-model ARNs; admit the
complete intended set explicitly. Reverify and release the application.

The Game Studio administrator must separately enable the chosen model, agent,
mode, workflow and paid execution in its saved Agent settings. The runtime role
allows `bedrock:InvokeModel` and `bedrock:GetInferenceProfile` only on its
operator-supplied ARN list, and the platform boundary further limits that list.
Changing a model target in the app cannot expand IAM or bypass either approval.
Configuration and deployment do not invoke a model. Rekognition image/video ML
permissions are not included in this first Core recipe; those optional media
workflow operations require a separately reviewed provider extension. Do not
claim them live-validated or enable them for students automatically.

AWS documents transactional condition permissions in
[Using IAM with DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html).
The existing source-build isolation, account checks, immutable source admission,
retention, recovery and charge-approval boundaries remain unchanged.


### Shared-runtime organization registrations

A reviewed Fargate application may host a bounded set of independently
registered Core organizations. Use the metadata-only
`configure_shared_application_registrations` operator command before preparing
the activation release. Its explicit deployment allowlist is part of the
reviewed binding snapshot; normal releases regenerate the existing Standard
SSM credential bundle from current Core records, preserving additional
registrations without reading old SSM data. Rotation updates the paired
encrypted deployment copy; stale, unavailable or mismatched credentials fail
closed. No IAM extension, new secret parameter, hosting resource or schema
change is required. See [shared-runtime registration
maintenance](external-applications.md#keep-shared-runtime-registrations-across-releases).
