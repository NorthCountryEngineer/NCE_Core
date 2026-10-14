# Deploy applications through Core

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
If it is empty or `None`, stop. Build, publish, and record the digest:

```sh
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
  --image "$NCE_IMAGE_URI" --enable-live 1 \
  --output-directory "$NCE_RELEASE_DIR"
```

The helper checks the account, stack state, and digest. It prepares updates
locally, preserves every other stack parameter, and adds the new operator switch.
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
`platform_services.0036... OK`, or already applied on a retry. If migration fails,
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

Open <https://main.dpn5tiolbymzn.amplifyapp.com/accounts/login/>. Sign in with
your existing account and confirm the deployment console opens before proceeding.
If you need to restore the old image, run the same `update-stack` command using
`core-cloud-recovery.json`, then its waiter. The recovery disables live deployment
and preserves the additive database schema. Do not regenerate keys or bootstrap
users. Service stability alone does not prove the public page is healthy.

### 16. Enter the platform settings once

Sign into your deployed Core administrator account. Open:

`https://main.dpn5tiolbymzn.amplifyapp.com/organizations/family/platform/cloud/platforms/`

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
