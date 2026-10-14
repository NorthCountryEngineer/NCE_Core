# Container application releases

The `amplify-fargate` recipe keeps Core as the identity and application control
plane. Its fixed template deploys Proposal Studio's API, database-backed worker
and isolated browser service on ECS Fargate, with private PostgreSQL, private
document storage and Amplify hosting. Core never executes application code on
its own host or in the deployment runner. Images are built separately; the
artifact-only source builder performs the npm frontend build without deployment
or runtime-secret permissions.

This recipe uses ARM64 images, port 8000, readiness path `/api/ready`, and fixed
`python -m studio.runtime api`, `worker` and `migrate` commands. The browser
image supplies its entrypoint and listens on port 8010. Application source
cannot provide arbitrary templates, roles, ports or commands.

## Repository and platform contract

The exact reviewed commit must contain root `nce-deploy.json`:

```json
{
  "schema": 1,
  "archetype": "amplify-fargate",
  "frontend": {"directory": "frontend", "output": "dist"},
  "backend": {
    "image": "ACCOUNT.dkr.ecr.REGION.amazonaws.com/PREFIX-api@sha256:64_HEX_DIGITS",
    "browser_image": "ACCOUNT.dkr.ecr.REGION.amazonaws.com/PREFIX-browser@sha256:64_HEX_DIGITS"
  },
  "identity": {
    "callback_path": "/api/auth/core/callback",
    "permissions": [
      {"codename": "workspace-use", "name": "Use Proposal Studio"}
    ]
  }
}
```

Replace image placeholders with actual immutable ECR digests. Permission
codenames must match the application's enforced mapping. Changing an installed
callback or permission vocabulary requires an explicit package upgrade; a
source release cannot silently change it. Core provides its reserved `admin`
permission separately; the manifest must not declare that reserved codename.

Follow the existing platform onboarding procedure, adding parameter
`EnableFargateRecipe=true` when intentionally funding this recipe. Preserve
existing platform parameters during an update. Record output
`FargateCloudFormationExecutionRoleArn` in **Container CloudFormation execution
role ARN**. Retain the existing static/serverless role in its existing field.
Pin the reviewed new Core runner commit, then release Core's backend and additive
migrations before exposing the new service approach.

In **Approved container resources**, enter a JSON map keyed by the exact approved
repository:

```json
{
  "NorthCountryEngineer/Proposal_Studio": {
    "image_repository": "ACCOUNT.dkr.ecr.REGION.amazonaws.com/PREFIX-proposal-api",
    "browser_image_repository": "ACCOUNT.dkr.ecr.REGION.amazonaws.com/PREFIX-proposal-browser",
    "runtime_secret_arn": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:PREFIX-proposal-runtime-SUFFIX",
    "certificate_arn": "arn:aws:acm:REGION:ACCOUNT:certificate/CERTIFICATE_UUID",
    "api_host": "api.proposalstudio.ai"
  }
}
```

References must belong to the platform's account and region. Image repositories
and bootstrap secret names must use the platform prefix to fit the role
boundary. Use a prefix of at most 10 characters, such as `nce-family`, so derived
load-balancer names fit AWS's limit. Provision the ECR repositories, stable
bootstrap secret and issued regional ACM certificate through separately reviewed
infrastructure. The bootstrap `session_secret` also protects encrypted provider
credentials. Credential values never belong in the map, manifest or frontend.

Create the Core application with final origin `https://proposalstudio.ai`.
Client placement is immutable; complete DNS and TLS at that origin before
testing sign-in. Core creates a public authorization-code + PKCE client and a
separate confidential introspection client, using the manifest's permissions.
Registration grants no ordinary user's membership or app permissions.

## Release and recovery

Provision creates a new stack with zero application tasks. For an existing stack
it preserves the running digests and desired count until migration. Core then
registers identity clients and stores their private resource bundle in this
application's SSM SecureString. The public configuration enters the runner
payload; credentials do not.

The release stops services and registers the chosen image digests. A dedicated
migration task runs on private subnets with public-IP assignment disabled, using
the fixed recipe command. Migration failure leaves services stopped. Success
permits activation; the runner waits for the API, singleton worker and browser
service to stabilize before publishing the independently built frontend.

ECS task definitions are immutable revisions and may be replaced. Other
resource replacement and deletion remain blocked by change-set inspection.
CloudFormation rollback cannot reverse database migrations. Use compatible
additive migrations and retain previous digests. For a failed migration inspect
its task log, repair it or restore compatible previous images, then deliberately
reactivate services. Do not drop data or run concurrent migrations blindly.

The application task role grants document storage access; the execution role
injects exact runtime/database secrets and Core's SSM bundle at startup. The
browser task has no application task role, database access or secret injection.
Network rules permit only the API/worker-to-browser connection. The source
builder receives none of these roles or credentials.

The public receipt includes hosting URLs and a validated identity contract.
Internal task, subnet, database and secret references stay inside the runner.
Cloudflare DNS is an operator step. Amplify's `/api` reverse rewrite must precede
the SPA fallback and reach the issued API TLS origin; verify cookies and CSRF
after deployment.

## Verification

```sh
python manage.py --local test platform_services.tests.test_cloud_fargate \
  platform_services.tests.test_cloud_aws platform_services.tests.test_cloud_deployments \
  platform_services.tests.test_cloud_portal platform_services.tests.test_cloud_recovery \
  platform_services.tests.test_cloud_release_config --noinput
python deployment/aws-platform/check_template.py
cfn-lint deployment/aws-platform/platform.template.json \
  deployment/aws-apps/fargate.template.json \
  deployment/aws-apps/fargate-bootstrap.template.json
cfn-guard validate --data deployment/aws-apps/fargate.template.json \
  --rules deployment/aws-apps/fargate.guard
cfn-guard validate --data deployment/aws-apps/fargate-bootstrap.template.json \
  --rules deployment/aws-apps/fargate.guard
```

Reproduce the deterministic runtime IAM baseline without uploading it:

```sh
uvx iam-policy-autopilot@0.3.0 generate-policies \
  "$PWD/deployment/aws-apps/runner.py" "$PWD/platform_services/cloud_aws.py" \
  --region us-east-1 --account 015454941623 \
  --service-hints ecs cloudformation codebuild s3 amplify ssm sts --pretty
```

Runtime variables require the pinned template's narrower resources, tags and
pass-role scopes. Offline checks supplement CloudFormation schema/security
validation and account-aware change-set inspection.

Production acceptance requires Core sign-in/logout and live permission
revocation, organization/document/result isolation, persistence,
uploads/downloads, provider-vault access and budget enforcement, authorized
low-cost inference, worker recovery, SAM.gov browser discovery, restart recovery,
backups and a verified previous-image recovery path. Offline tests do not
establish those live outcomes. Controlled government information remains
prohibited by Proposal Studio's application contract.

References: [ECS execution-role secret injection](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html),
[ECS deployments](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-type-ecs.html),
[AWS Service Authorization Reference](https://docs.aws.amazon.com/service-authorization/latest/reference/).
