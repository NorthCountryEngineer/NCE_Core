# Application OCI/Helm releases

This is an additional deployment strategy. It does not change the existing NCE Core Kubernetes self-deployment, CloudRelease, Amplify, CodeBuild/CloudFormation or Fargate recipes.

## Developer and release workflow

1. Push the application commit to its private repository and obtain a successful required GitHub Actions job on that exact commit. Core's own required checks must pass on the exact trusted runner commit.
2. On an approved NCE-owned builder, use the reviewed config in ci/applications and an exact-commit checkout, then run: python ci/pipeline.py application-release --config ci/applications/proposal-studio.example.json --source ../Proposal_Studio --mode verify
3. Verification generates a Helm chart with SYNTHETIC digest placeholders, runs Helm lint/template/kubeconform, and packages the chart. It does not build actual containers, publish anything, or test a live deployment.
4. The analogous --mode publish execution requires the CORE_CI_ALLOW_PUBLISH=1 switch, an exactly matching CORE_CI_ENVIRONMENT, a clean runner matching the approved release branch tip, and exact application and runner CI receipts. It then builds and pushes production images, reads immutable registry SHA256 digests, renders and validates the same release chart, pushes it as an OCI artifact, signs artifacts when configured, generates SBOMs when configured, and records its release receipt locally and in S3. No Raft deployment occurs.

The manual GitHub Actions release entrypoint is .github/workflows/application-oci.yml. Its source checkout uses an NCE-managed, narrowly scoped GitHub App token with read access to Proposal_Studio. Core CI admission independently requires protected read-only access to both apps' GitHub workflow results (CORE_CI_GITHUB_TOKEN_FILE or equivalent). Neither credential goes to Raft.

The runner requires Python 3.12/jsonschema, Git, Docker Buildx, Helm 3, kubeconform, AWS CLI v2, and (if enabled) syft and cosign. Credentials must come from short-lived NCE build-runner role sessions; never embed credentials in repo configurations. Provision the ECR image and chart repositories and S3 release receipt bucket under operator approval first. The example config contains fake account 000000000000 and a placeholder bucket: replace these along with DNS/TLS settings before any authorized publish.

## Artifact and [R]AP contract

The release manifest contains source and runner commits, exact CI receipts, image references pinned as repository@sha256, the versioned chart OCI reference plus its separate registry manifest digest, the chart-package SHA256, schema/render validation and stage statuses. The chart version derives from semantic version, application commit and trusted runner commit. An S3 receipt is created with conditional write, not silently overwritten. The sample requires cosign and syft. Independently verify attestations in Raft's security pipeline.

[R]AP receives image and chart OCI references plus sanitized metadata only. It receives no source, Git history, developer credentials or build scripts. Raft owns its namespace, Secrets, DNS, TLS, networking, image-pull role, approvals, admission policies and Argo CD sync. Set the Argo CD Helm OCI repoURL to the ECR chart repository parent, chart to proposal-studio and targetRevision to the published immutable chart version; Argo CD resolves chart versions rather than NCE's recorded chart digest, so enable ECR tag IMMUTABILITY and prevent artifact deletion/overwrite. Avoid 'latest' tags.

The chart provides frontend (8080), FastAPI (8000) and isolated Chromium (8010) Deployments/Services, read-only security contexts, scoped NetworkPolicies, ServiceAccounts, probes, resource constraints, ingress and TLS, plus a one-shot migration Job with Argo CD PreSync and Helm pre-upgrade hooks. The migration entrypoint reuses the application's additive startup migrations. Back up Postgres/S3 and drain in-process jobs before any upgrade; do not assume reversible database changes. The API supports only one worker/replica and Recreate rollouts; there is no separate background worker until durable queues are implemented. The browser service likewise uses one replica.

PostgreSQL and S3 storage are external. Existing Kubernetes Secrets supply DATABASE_URL, SESSION_SECRET and any optional OAuth/S3 keys. Deployment-only values supply APP_URL, AUTH_MODE, S3 endpoint/region/bucket, ingress and ingress-controller label selectors. Use S3 role auth where approved; no Secrets are created by the chart. The reference-library PVC is optional and read-only. Browser public egress is explicitly opt-in; enforce private-host restrictions with the chosen CNI/egress proxy, because standard NetworkPolicy cannot guarantee URL/FQDN filtering. Specify approved CIDRs for database, S3 and model-provider egress, and review each customer infrastructure boundary.

## External feedback foundation

ci/oci_feedback.py provides normalize_feedback(release,event). It rejects raw logs and unsanitized fields, mismatched release/chart/image digests, unsupported failure/status classifications and unsafe diagnostic URLs. It accepts release/deployment identifiers, environment, target platform, stage/status, enumerated failure classification, opaque diagnostic reference, and human remediation category. Full external ingestion is NOT enabled: a future authenticated API must identify/authorize the [R]AP sender, prevent replay, audit the event and persist it against the NCE release of record. Do not send customer data, CUI or source code through diagnostics.

## Tests and limits

Run python -m unittest ci.tests.test_oci_release -v for the offline security, identity, digest, chart-generation, OCI/feedback and failure-mode tests. Helm rendering/schema checks require Helm/kubeconform installed on a runner. The dry-run's synthetic image digests are NOT evidence of real built images or live deployment health. CloudRelease model integration, registered feedback endpoint, SBOM/attestation distribution, and Raft approval/promotion remain separately tracked dependencies.
