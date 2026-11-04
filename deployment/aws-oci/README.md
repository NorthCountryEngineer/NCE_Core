# NCE Proposal OCI infrastructure

Reviewed snapshots for the existing `application-release` pipeline. Account `015454941623`, region `us-east-1`; builder `nce-proposal-oci-release`; no runtime/customer service changes.

Reuse the existing CodeConnections connection and versioned SSE-S3 bucket `nce-family-cloud-platform-artifacts-u7go6eehrq5w`, plus immutable `nce-family-proposal-api` and `nce-family-proposal-browser` repositories. Added immutable `nce-proposal-web` and `nce/proposal-studio`, a scoped CodeBuild role/project, OIDC dispatcher, asymmetric signing key and separate encrypted14-day build-log key/group. See [release operations](../../docs/helm-oci-releases.md).

The committed project/role snapshots deliberately omit the one-release CI credential selector/grant. Before an authorized future run, provision an approved encrypted CI admission credential, add its exact ARN to the role as `secretsmanager:GetSecretValue`, and set `NCE_CI_READ_SECRET_ARN` on the project. Do not copy the operator's broad GitHub credential without explicit permission. Remove the selector/grant and delete temporary secrets afterward. No token appears in these files.

GitHub environment `nce-oci-production` must restrict deployments to branch `main`. The dispatcher obtains an OIDC session of at most7200seconds; the role has no registry, S3, source-token or KMS permissions. IAM denies unapproved buildspec/source/role/image and environment overrides. Build role has no production database, customer object, runtime deployment, IAM mutation or artifact deletion rights. The connection's native token is scoped to the existing NCE integration; its installation repository permissions remain an upstream trust boundary, while UseConnection requests are limited to the two release repositories/read actions.

The dispatcher trust binds the exact immutable subject `repo:NorthCountryEngineer@132168834/NCE_Core@1412449668:environment:nce-oci-production` and audience `sts.amazonaws.com`. Verify the repository prefix using `GET /repos/NorthCountryEngineer/NCE_Core/actions/oidc/customization/sub`; the recorded response enables immutable subjects. Keep the exact match and environment restriction. See the [release authority documentation](../../docs/helm-oci-releases.md#release-authority-and-source-admission) for discovery and GitHub's subject-format reference.

These files describe infrastructure only. Publication always invokes the existing reviewed `ci/pipeline.py application-release` entrypoint, with exact successful application and Core CI evidence.
