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

Private browser discovery uses a Cloud Map namespace. Its CloudFormation
provider requires Route53 hosted-zone creation and metadata reads in addition
to Cloud Map calls. A separate managed policy attaches only to the Fargate
execution role and allows zone creation only when `aws:ViaAWSService` is true
and `aws:CalledViaLast` identifies `servicediscovery.amazonaws.com`. Missing
delegation keys, direct Route53 requests and other last-service identities are
denied. This chain constraint does not prove a VPC condition was present in the
delegated request: private-only admission also relies on the role allowing
`CreatePrivateDnsNamespace` while denying `CreatePublicDnsNamespace` and on the
fixed private-namespace template. Zone deletion uses the same FAS conditions
and a hosted-zone ARN pattern; direct zone operations and DNS record mutations
remain denied. Hosted-zone listing is read-only provider metadata. Newly created
namespace/service tags use the approved request tag; existing resources retain
their resource-tag checks. This preserves the role's inline-policy size limit.

AWS documents [Cloud Map FAS support](https://docs.aws.amazon.com/cloud-map/latest/dg/security_iam_service-with-iam.html)
and the [IAM forward-access condition keys](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_forward_access_sessions.html).
Offline context tests verify the allow/deny boundary; actual delegated-call
success must be verified during deployment.

Rollback also needs exact cleanup actions. `s3:DeleteBucketPolicy` remains in
the existing named platform-bucket grant. `ecs:DeregisterTaskDefinition` does
not support IAM resource ARNs, so a separate managed policy allows only this
action on `*` in the configured region and attaches only to the fixed Fargate
CloudFormation role. No task-family restriction is asserted for this action.
Normal Cloud Map namespace deletion retains its namespace ARN and resource-tag
checks; a failed resource that never existed does not justify weakening them.

The fixed providers also require metadata reads whose IAM actions do not support
resource ARNs: RDS engine defaults/events, ECS task definitions, log index
policies and Cloud Map tags. A separate managed policy grants those exact reads
on `*` only in the platform region and only to the Fargate CloudFormation role.
ECS service-deployment listing stays scoped to the platform's named services.
Database writes and application roles retain their existing resource scopes.
The app and branch both carry explicit platform/application tags, as do all
fixed resources whose create calls require the platform request tag. Branch
creation cannot rely on tags inherited from its Amplify app.

### Optional owner-bound frontend publishing

A live manual publish returned `AccessDenied` for `amplify:CreateDeployment`
even though the selected app and branch both carried the expected platform and
application tags. AWS's [Amplify Service Authorization Reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amplify.html)
documents branch ARN scoping and resource-tag conditions for `CreateDeployment`
and `StartDeployment`. The observed result suggests a tag-context mismatch for
that call; it does not establish an AWS service defect or justify a broad write
grant.

The subsequent live CodeBuild call at 22:53 UTC on October 14, 2026 still
returned `AccessDenied` when the owner binding included only the exact
production branch ARN. At 22:58 UTC, a diagnostic under the same CodeBuild
runner role succeeded after adding that branch's `/*` descendants, with the
app, branch, two actions and region unchanged. It created only an unstarted
deployment job; it performed no artifact upload or `StartDeployment`. CloudTrail
did not expose the internally evaluated resource ARN, so this observation
establishes the working scope without identifying a particular internal ARN.

The onboarding template provides an optional owner-managed binding. After
provisioning, verify the actual Amplify app ID, branch, stack ownership, account,
region and both `nce:platform-prefix` and `nce:application` tags against the
approved application. Before retrying frontend publication, update only the
platform stack's `FrontendAppId` to that verified ID and `FrontendBranch` to the
exact approved branch, preserving all other parameters. The branch defaults to
`production`; the app ID defaults to empty, which creates no additional policy.
These are platform-owner inputs, never application-source or release-manifest
values. Do not bind another tenant's app merely because its name is familiar.

A separate conditional managed policy attaches only to the trusted runner and
permits exactly `amplify:CreateDeployment` and `amplify:StartDeployment` on that
single app/branch ARN and its `/*` descendants in the platform account and
region. The parameters reject wildcards and branch paths; only the template
adds the descendant suffix after the literal approved branch. The grant uses
that binding and requested region without a resource-tag condition; the existing
tag-conditioned reads and normal publish grant remain intact. It grants no
application edits, other apps or branches, CodeBuild, role, secret or data access.
Clear `FrontendAppId` to remove the
optional grant. This policy-only change requires no runner-code or Core-runtime
update; keep the reviewed runner commit pin unchanged.

Create the Core application with final origin `https://proposalstudio.ai`.
Client placement is immutable; complete DNS and TLS at that origin before
testing sign-in. Core creates a public authorization-code + PKCE client and a
separate confidential introspection client, using the manifest's permissions.
Registration grants no ordinary user's membership or app permissions.

The template makes `ApplicationOrigin` the canonical HTTPS host and permanently
places a 301 redirect from `https://www.<configured-host>` first in Amplify's
rules, before the `/api` and `/integrations` proxies and SPA fallback. Its
domain-only source preserves request paths and query parameters; the origin
constraint rejects ports, paths, credentials, queries and fragments. The rule
becomes active when the operator associates the `www` alias and configures DNS
and TLS for it. Domain association and Cloudflare records remain operator steps.
See [Amplify domain redirect behavior](https://docs.aws.amazon.com/amplify/latest/userguide/redirect-rewrite-examples.html)
and [Cloudflare domain setup](https://docs.aws.amazon.com/amplify/latest/userguide/to-add-a-custom-domain-managed-by-cloudflare.html).

## Release and recovery

Provision creates a new stack with zero application tasks. For an existing stack
it preserves the running digests and desired count until migration. Core then
registers identity clients and stores their private resource bundle in this
application's SSM SecureString. The public configuration enters the runner
payload; credentials do not.
Both serverless and Fargate provision phases persist this SecureString before
the release phase can start a migration or application task that injects it.

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

The database's VPC, both private subnets, security group, subnet group and
parameter group have `DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain`
along with the database and its credential secret. Retaining this dependency
chain keeps a failed creation or rollback from attempting to remove resources
still used by the protected database. A failed release may therefore leave
retained infrastructure and its normal AWS charges. Recover it through reviewed
resource imports using the existing physical identifiers and all original
parameters, then apply the fixed recipe at zero task count before retrying Core's
normal release. Preserve deletion protection and reject data/network replacement
or deletion. During import, keep already managed resources and stack settings
unchanged. If rollback already removed an unused subnet, capture the subnet
group's actual identifiers in the import seed; the next update can create the
missing subnet and modify the existing subnet group in place. See
[CloudFormation manual imports](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/import-resources-manually.html)
and [RDS subnet-group update behavior](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-rds-dbsubnetgroup.html).

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
