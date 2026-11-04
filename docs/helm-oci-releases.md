# Application Helm OCI releases

The existing `ci/pipeline.py application-release` command is the NCE release system. The AWS adapter runs that same command in the isolated `nce-proposal-oci-release` CodeBuild project; it does not replace Amplify, Fargate, Lightsail or existing Core deployment recipes.

## Release authority and source admission

The manual `.github/workflows/application-oci.yml` workflow dispatches only from `main` through the `nce-oci-production` GitHub environment. That environment accepts only the `main` branch. GitHub OIDC grants a short-lived dispatcher session with access to the fixed CodeBuild project, not ECR or signing. IAM rejects buildspec, source, role, image and unauthorized environment overrides. Direct AWS operators retain their own authority and must use the reviewed dispatcher/inputs.

AWS CodeConnections downloads the exact Core and private application revisions into NCE infrastructure. The builder checks both Git revisions and requires successful required **push** CI on both exact commits. Publication also requires the current approved Core release-branch tip, a clean checkout, matching production environment, the fixed NCE CodeBuild ARN and correct STS account. A newer pending/failed CI attempt remains a refusal. No credentials, source checkout, Git history or developer source archive go to the deployment consumer. Executable images contain the application's normal runtime modules.

The production configuration is `ci/applications/proposal-studio.production.json`. It names verified NCE ECR repositories, existing receipt storage, the builder and immutable KMS signing key. `ci/applications/proposal-studio.validation.json` contains synthetic destination bindings solely for lint/schema checks; those bindings are excluded from the packaged chart.

The current release uses an explicitly operator-authorized temporary Secrets Manager credential for private CI admission reads, written to a mode-0600 temporary file. Its original OAuth scope is broader than Actions read; it is not described as a read-only token. Delete the AWS secret and remove its role grant/project selector after this release. Future releases must provision an approved Actions-read credential before dispatch; the normal live-platform CI credential never falls back to this build-specific transport. No credentials are included in receipts, buildspecs or container arguments.

## Publication and evidence

1. Install checksum-pinned Helm 3, kubeconform, Cosign and Syft. Validate a synthetic preview with strict Helm lint, template and Kubernetes schemas before publication; unknown schemas fail.
2. Before registry writes, verify all four repositories enforce `IMMUTABLE` with no exclusions, the release identity is absent, receipt-bucket region matches, and the KMS key can sign and locally verify a challenge.
3. Build all three production images on NCE AWS Linux amd64, with exact application/runner revision labels. Run isolated disposable PostgreSQL migration, non-root/read-only API, Chromium, frontend health and API proxy checks before the first push. These containers receive synthetic state, no host mounts/customer services/build credentials, and no public ports.
4. Publish images under the version derived from both commits. Resolve ECR manifests, verify their content SHA-256, pull by digest and verify source/builder labels and architecture.
5. Generate the chart from those exact digests, validate, package and push to ECR OCI. Resolve ECR's chart manifest independently of Helm output, check Helm media types and package-layer digest, and retrieve the package by immutable digest.
6. Sign all four immutable OCI artifacts with the NCE asymmetric KMS key, then verify every signature. Produce SPDX JSON inventories for all three images; upload/hash/read back each inventory, attach signed SPDX attestations and verify their exact image subject and inventory.
7. Upload the public verification key, signed receipt and bounded evidence to versioned/encrypted S3 using conditional create-only writes and content readback. The final receipt appears only after all required evidence succeeds. A partial publication never qualifies as a completed release and cannot overwrite existing identities.

Private release metadata is not sent to a public transparency log. Verification deliberately trusts the independently supplied NCE KMS public key and uses `cosign verify --key signing-public.pem --insecure-ignore-tlog=true IMAGE@sha256:DIGEST`; this flag skips a public-log requirement, not signature verification. Verify SPDX with `cosign verify-attestation --key signing-public.pem --insecure-ignore-tlog=true --type spdxjson IMAGE@sha256:DIGEST`. Verify the receipt bytes with `cosign verify-blob --key signing-public.pem --insecure-ignore-tlog=true --signature release.json.sig release.json`. Confirm the public-key fingerprint through a separate trusted NCE channel before first use. The receipt records the public-key hash and immutable evidence locations.

## Destination contract

The chart URI/version and separate ECR manifest digest, chart-package digest, application/runner commits, pinned images, CI receipts, runtime/schema evidence, verification key and signed manifest form the handoff. Chart values.schema.json locks the exact image/release identities so overlays cannot silently replace the software.

For Argo CD, use the chart repository parent as `repoURL` (without `oci://`), `chart: proposal-studio`, the exact published `targetRevision`, and enable OCI for the registry credential. Argo CD resolves the version tag; ECR immutability and access policies must prevent overwrite/deletion. Verify NCE's recorded chart digest independently before promotion. Helm can pull the recorded digest directly. The destination owns its read-only registry role/token renewal, namespace, secret management, DNS/TLS, policy engine, Argo CD sync and production approvals. No destination deployment is performed by this publisher.

The chart packages web8080, API8000 and isolated browser8010 workloads, service accounts, strict security contexts, probes, resource constraints and NetworkPolicies. API/browser are one replica; API uses one worker and Recreate rollout. It does not invent a distributed worker. External PostgreSQL and S3-compatible storage remain destination bindings. The destination must supply `DATABASE_URL`, `SESSION_SECRET`, authentication/storage settings and approved provider credentials through existing Secrets. It supplies origins, ingress/TLS, image-pull configuration and approved external egress CIDRs separately. The reference-library PVC is optional/read-only. Standard NetworkPolicy does not guarantee FQDN filtering; browser public egress is explicit opt-in and needs destination CNI/proxy review.

Before rollout, the destination must back up PostgreSQL/object data, drain in-process jobs and verify recovery. An earlier hook creates the migration service account and dedicated egress policy before the Argo PreSync/Helm migration Job. `python -m studio.migrate` runs once before rollout; a PostgreSQL advisory lock serializes this command with API startup. Failed migrations do not retry automatically. Validate recovery and authenticated functionality in the destination before declaring deployment complete.

## Feedback and validation limits

`ci/oci_feedback.py` validates sanitized release-correlated status events; an authenticated, replay-protected persistent feedback endpoint remains a separate integration. NCE owns artifact release state; the destination remains authoritative for deployment/runtime state.

Run `python -m unittest discover -s ci/tests -v` for offline contracts, chart regressions and fail-closed publication checks. Core required CI also runs disposable PostgreSQL application tests, image build and real strict chart validation. `--mode verify` uses synthetic digests and does not build/publish production containers or prove a live destination deployment.
