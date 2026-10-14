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

HTTP APIs use API Gateway QuickCreate with a Lambda target. The execution role
has no broad write access to HTTP API routes/integrations, whose tags are not
inherited from the parent HTTP API. Amplify applications and branches carry the
platform prefix tag. Amplify's create API and CloudWatch log-group discovery
require `Resource: "*"`; those statements use request-tag or region conditions.
If manual frontend publication fails despite verified app/branch tags, the
platform owner can optionally bind `FrontendAppId` and `FrontendBranch` to one
verified app/branch after provisioning. Empty app ID disables the separate
runner-only two-action grant. See [owner binding and validation](../../docs/cloud-fargate.md#optional-owner-bound-frontend-publishing).

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
