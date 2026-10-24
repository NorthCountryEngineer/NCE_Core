# AWS deployment platform onboarding

`platform.template.json` is a **user-run** CloudFormation template for a single
Core organization's AWS deployment destination. It creates a private encrypted
artifact bucket, two distinct CodeBuild projects, their scoped roles, a bounded
CloudFormation execution role, an application permissions boundary, and a Core
control role. Creating the stack starts no builds, but storing artifacts and
logs and later builds/deployments can incur AWS charges.

Read [the complete onboarding and validation walkthrough](../../docs/cloud-deployments.md)
before creating resources. Complete the AWS CodeConnections GitHub handshake in
AWS first. Restrict the GitHub App installation to the selected repositories.
The connection must be `AVAILABLE`, in the target account and region.

The application source project executes repository build commands and can only
write `builds/` archives, use the source connection, and write its own logs. It
has no deployment, SSM, or IAM permissions. The deployment project runs only
trusted Core code. Its inline buildspec verifies `RunnerCommit` before installing
trusted requirements or running Python. Core receives short-lived STS control
credentials and stores no AWS access keys or GitHub personal access token.

The runner uses Linux Small compute and the source builder uses Linux Medium.
The runner remains active while awaiting the build, so the account must have
one available concurrent build slot for each type. Using distinct compute types
avoids both jobs competing for a single Small quota. Check the account's applied
[CodeBuild concurrency quotas](https://docs.aws.amazon.com/codebuild/latest/userguide/limits.html)
before live deployment. Both running projects incur build charges.

The control role trust and attached Core policy require the same external ID.
`CoreRuntimeRoleArn` identifies the existing ECS **task role**, not the task
execution role. `CoreRuntimeRoleName` must name that exact role. Set
`AttachCoreRuntimePermission=false` only when another owner explicitly manages
and installs the same scoped `sts:AssumeRole` grant.

Application stacks create their own Lambda runtime role with this mandatory
permissions boundary. Each role's inline permissions reference only that app's
DynamoDB tables, media bucket, log group, and Core credential parameter. The
CloudFormation role cannot attach managed policies or remove the boundary; it
may pass app roles only to Lambda. Prefixes must be unique across tenants.

The boundary permits credential decryption only through Parameter Store with
the AWS managed `alias/aws/ssm` key and a matching app credential encryption
context. The control role can encrypt that credential path, and each app's
inline role narrows decryption to its exact parameter. No KMS key is created;
customer-managed credential keys require a separate reviewed extension.

The generic serverless recipe uses API Gateway QuickCreate with a Lambda target.
Game Studio instead declares an explicit payload-version-2 integration, default
route and throttled default stage. Its execution role permits `apigateway:POST`
only on `/apis/*/integrations`, `/apis/*/routes` and `/apis/*/stages`, requiring
both `aws:ResourceTag/nce:platform-prefix` equal to this platform prefix and this
AWS region. Missing tags or region deny access; there is no untagged fallback.
It adds no create access to authorizers, deployments, models or other collections,
and no new role-passing authority.

The [API Gateway V2 authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_apigatewayv2.html#apigatewayv2-actions-as-permissions)
explicitly lists the resource-tag condition for POST on these three collection
resources. The [tagging guide](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-tagging-supported-resources.html)
lists direct V2 taggable resources and documents inheritance for V1; that is not
proof of physical tag inheritance by V2 child resources. The role uses the V2
reference's documented authorization context and fails closed if it is absent.
Offline policy tests verify this scope, while an authorized provider retry must
verify the actual context before a release is accepted.

Stage tag-on-create additionally needs the literal `apigateway:TagResource`
action on `/apis/*/stages`. The dedicated grant requires the existing platform
resource tag, the requested platform tag and this AWS region. It permits no
stage-object tagging path, `PUT`, `UntagResource` or missing-tag fallback.
This follows the current us-east-1 CloudFormation Stage provider schema, whose
create permissions are `apigateway:POST` and `apigateway:TagResource`, and the
observed tagged `CreateStage` denial. The schema is available through
`aws cloudformation describe-type --type RESOURCE --type-name AWS::ApiGatewayV2::Stage`
or the [AWS-published resource schema archive](https://schema.cloudformation.us-east-1.amazonaws.com/CloudformationSchema.zip)
(`aws-apigatewayv2-stage.json`). The authorization reference currently maps the
`TagResource` operation to verb actions but omits this literal action. That
documentation discrepancy does not justify widening the grant to all API
Gateway writes; an authorized provider canary must verify these exact required
conditions. The schema's update handler also lists `UntagResource`; this change
does not authorize that operation or claim stage retagging support.

Amplify applications and branches carry the platform prefix tag. Amplify's
create API and CloudWatch log-group discovery require `Resource: "*"`; those
statements use request-tag or region conditions.
If manual frontend publication fails despite verified app/branch tags, the
platform owner can optionally bind `FrontendAppId` and `FrontendBranch` to one
verified app/branch after provisioning. Empty app ID disables the separate
runner-only two-action grant, which covers only that literal branch ARN and its
`/*` descendants in the platform account and region. On October 14, 2026, a
22:53 UTC live CodeBuild publish was denied with branch-only scope; a 22:58 UTC
diagnostic under the same role succeeded after adding only the selected branch's
descendants. AWS did not disclose the internally evaluated ARN. See
[owner binding and validation](../../docs/cloud-fargate.md#optional-owner-bound-frontend-publishing).
Cancel every unstarted diagnostic job with `StopJob` and verify `CANCELLED`
before optional deletion; deleting a job alone is not cancellation evidence.

CloudFormation's S3 read handler inspects bucket metadata beyond the properties
declared in a template. Its execution role therefore includes those metadata
reads and `ListBucket` on the application bucket name prefix, plus the IAM role
read actions needed to inspect an inline-only runtime role. These are based on
the [public regional CloudFormation resource schemas](https://schema.cloudformation.us-east-1.amazonaws.com/CloudformationSchema.zip);
they do not grant object access to application media.

Artifacts and app data are retained on stack deletion. `builds/` source archives
expire after 7 days, `runs/` receipts after 90 days, and logs after 14 days. Backend
`code/` archives remain because Lambda deployments reference those objects.
Disabling a platform prevents subsequent Core deployments; it does not delete
billable AWS resources.

The JSON template is below CloudFormation's 51,200-byte inline limit and includes
architecture context. Local structure checks do not prove IAM or account-specific
service permissions. Run `aws cloudformation validate-template` before onboarding,
and inspect the first change set. No AWS deployment was performed while creating
these sources.


### Game Studio manual frontend binding

The observed Game Studio `CreateDeployment` denial names an internal
`apps/<app>/branches/<branch>/deployments/*` resource even though app and branch
ownership tags are present. The existing tag-conditioned wildcard already
matches that path; adding another wildcard under the same tag condition cannot
supply missing service tag context. The [Amplify authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amplify.html)
lists branch tags for CreateDeployment/StartDeployment; the [API paths](https://docs.aws.amazon.com/amplify/latest/APIReference/API_CreateDeployment.html)
and the actual denial identify the narrower deployment collection used here.

After provisioning, the platform owner must verify the exact app and branch
against the Game Studio stack outputs, account, region, and both
`nce:platform-prefix` and `nce:application` tags. Set only
`GameStudioFrontendAppId` and `GameStudioFrontendBranch` to those reviewed
values. Empty app ID is the default and creates no policy. These parameters
are operator inputs, never repository or release-manifest inputs. Preserve the
existing `FrontendAppId`/`FrontendBranch` binding for other applications.

The separate conditional policy attaches only to RunnerRole and grants only
CreateDeployment and StartDeployment on that literal branch ARN and its
`deployments/*` and `jobs/*` descendants in the platform account and region. Ownership is
established by the verified exact binding because the observed internal
resource does not supply the tag context used by the normal policy. The
normal tag-conditioned read/publish policy remains unchanged. No untagged
wildcard publishing authority is added. Clear GameStudioFrontendAppId to
remove this optional grant. Runner source, Core runtime and CI policy need no
change to apply this operator policy correction.

StartDeployment uses the deployment API path and returns a job summary.
The authorization reference also requires branch scope; the exact binding
therefore includes that branch and only its deployment/job descendants for
the two publish actions, preserving the earlier working owner-bound lifecycle. The existing GetJob/StopJob
statement already covers the documented exact account/region
`apps/*/branches/*/jobs/*` path; those job resources do not support resource-tag
conditions in the authorization reference. This change adds no GetJob/StopJob actions or
resource scope. Offline tests check wrong app, branch, account, region, missing
region, and unrelated-action refusal. A real controlled publication must still
verify AWS's context and complete CreateDeployment, upload, StartDeployment
and GetJob success. Cancel any unstarted diagnostic job with StopJob and
verify CANCELLED before another release; do not stop a real publication or
another application's job.

### Detailed role architecture context

The following unchanged context was moved out of the inline template to keep
it below CloudFormation's 51,200-byte body limit. Each resource retains a
metadata reference to this section; policies, trust, retention and parameters
retain their original semantics.

#### AppBoundary architecture context

Bound app runtime permissions, including conditional transaction checks, to platform namespaces.

- CloudFormation must require this boundary on every new app role.
- No IAM, STS, deployments, or account administration.
- Container execution permissions stay inside named platform image, secret and log namespaces; runtime inline policies select exact app resources.
- Paid Game Studio models require explicit exact model/profile ARNs; the default boundary grants none.

#### FargateCloudFormationRole architecture context

Container provisioning is separate from the existing serverless execution role; only pinned Core templates can use it.

- Use platform-tagged network resources and named container/database resources.
- New task and execution roles require the platform boundary.
- No managed-policy attachment, boundary removal or application-code execution.
- Application roles cannot invoke network or DNS control operations.
- Private namespace support is separate; Cloud Map FAS zone lifecycle only, no record mutation.

#### RunnerRole architecture context

Only pinned Core runner may provision and publish; never execute application code here.

- Fixed inline buildspec verifies Core Git commit before Python execution.
- App source is data; builder executes it under a distinct role.
- Migration task definitions and roles stay in the platform namespace; migration command is fixed by the pinned recipe.

#### ControlRole architecture context

Core receives short-lived control credentials; store only role identifiers in settings.

- Trust exactly the existing Core runtime IAM role and deployment-specific external ID.
- Core cannot modify IAM or CloudFormation directly.

#### CoreAssumeControl architecture context

Complete the two-sided assume-role grant on the existing Core runtime role.

- Grant only this new control role and matching external ID.
- Attach to task role, never task execution role or account root.

#### SourceRole architecture context

Application install/build commands are untrusted; isolate them from deployment credentials.

- No CloudFormation, Lambda, SSM, IAM pass-role, or Amplify permissions.
- Source role only writes source archives under builds/.
