# Application Helm OCI releases

The existing `ci/pipeline.py application-release` command is the NCE release system. The AWS adapter runs that same command in the isolated `nce-proposal-oci-release` CodeBuild project; it does not replace Amplify, Fargate, Lightsail or existing Core deployment recipes.

## Release authority and source admission

The manual `.github/workflows/application-oci.yml` workflow dispatches only from `main` through the `nce-oci-production` GitHub environment. That environment accepts only the `main` branch. GitHub OIDC grants a short-lived dispatcher session with access to the fixed CodeBuild project, not ECR or signing. IAM rejects buildspec, source, role, image and unauthorized environment overrides. Direct AWS operators retain their own authority and must use the reviewed dispatcher/inputs.

The dispatcher trust requires exact `StringEquals` matches for audience `sts.amazonaws.com` and subject `repo:NorthCountryEngineer@132168834/NCE_Core@1412449668:environment:nce-oci-production`. This repository uses [GitHub immutable subject claims](https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims). Discover its subject prefix with `GET /repos/NorthCountryEngineer/NCE_Core/actions/oidc/customization/sub`: the verified response has `use_default: true`, `use_immutable_subject: true` and `sub_claim_prefix: repo:NorthCountryEngineer@132168834/NCE_Core@1412449668`. Preserve the exact environment suffix and the environment's `main` branch restriction; never replace the subject with a wildcard or change GitHub's OIDC settings to accommodate an outdated trust policy.

AWS CodeConnections downloads the exact Core and private application revisions into NCE infrastructure. The builder checks both Git revisions and requires successful required **push** CI on both exact commits. Publication also requires the current approved Core release-branch tip, a clean checkout, matching production environment, the fixed NCE CodeBuild ARN and correct STS account. A newer pending/failed CI attempt remains a refusal. No credentials, source checkout, Git history or developer source archive go to the deployment consumer. Executable images contain compiled application runtime modules. Proposal Studio's proprietary Python modules use CPython3.12 adjacent bytecode, with original `.py` files excluded from clean final image stages. Licensed third-party Python dependencies and runtime assets remain. Bytecode is inspectable and decompilable; this is object-code packaging, not encryption or an anti-reverse-engineering guarantee.

The production configuration is `ci/applications/proposal-studio.production.json`. It names verified NCE ECR repositories, existing receipt storage, the builder and immutable KMS signing key. `ci/applications/proposal-studio.validation.json` contains synthetic destination bindings solely for lint/schema checks; those bindings are excluded from the packaged chart.

The current release uses an explicitly operator-authorized temporary Secrets Manager credential for private CI admission reads, written to a mode-0600 temporary file. Its original OAuth scope is broader than Actions read; it is not described as a read-only token. Delete the AWS secret and remove its role grant/project selector after this release. Future releases must provision an approved Actions-read credential before dispatch; the normal live-platform CI credential never falls back to this build-specific transport. No credentials are included in receipts, buildspecs or container arguments.

## Publication and evidence

1. Install checksum-pinned Helm 3, kubeconform, Cosign and Syft. Validate a synthetic preview with strict Helm lint, template and Kubernetes schemas before publication; unknown schemas fail.
2. Before registry writes, verify all four repositories enforce `IMMUTABLE` with no exclusions, the release identity is absent, receipt-bucket region matches, and the KMS key can sign and locally verify a challenge.
3. Build all three production images on NCE AWS Linux amd64, with exact application/runner revision labels. Run isolated disposable PostgreSQL migration, non-root/read-only API, Chromium, frontend health and API proxy checks before the first push. These containers receive synthetic state, no host mounts/customer services/build credentials, and no public ports. Before any image push, inspect the API/browser compiled payload and frontend output, and inspect every final OCI layer so a later deletion cannot hide original application source. Require valid current-interpreter bytecode and reject original application Python, Git/test/cache trees and frontend TypeScript/source maps; retain normal compiled JavaScript and licensed vendor code.
4. Publish images under the version derived from both commits. Resolve ECR manifests, verify their content SHA-256, pull by digest and verify source/builder labels and architecture.
5. Generate the chart from those exact digests, validate, package and push to ECR OCI. Resolve ECR's chart manifest independently of Helm output, check Helm media types and package-layer digest, and retrieve the package by immutable digest.
6. Sign all four immutable OCI artifacts with the NCE asymmetric KMS key, then verify every signature. Produce SPDX JSON inventories for all three images; upload/hash/read back each inventory, attach signed SPDX attestations and verify their exact image subject and inventory.
7. Upload the public verification key, signed receipt and bounded evidence to versioned/encrypted S3 using conditional create-only writes and content readback. The final receipt appears only after all required evidence succeeds. A partial publication never qualifies as a completed release and cannot overwrite existing identities.

Private release metadata is not sent to a public transparency log. Verification deliberately trusts the independently supplied NCE KMS public key and uses `cosign verify --key signing-public.pem --insecure-ignore-tlog=true IMAGE@sha256:DIGEST`; this flag skips a public-log requirement, not signature verification. Verify SPDX with `cosign verify-attestation --key signing-public.pem --insecure-ignore-tlog=true --type spdxjson IMAGE@sha256:DIGEST`. Verify the receipt bytes with `cosign verify-blob --key signing-public.pem --insecure-ignore-tlog=true --signature release.json.sig release.json`. Confirm the public-key fingerprint through a separate trusted NCE channel before first use. The receipt records the public-key hash and immutable evidence locations.

## Destination contract

The chart URI/version and separate ECR manifest digest, chart-package digest, application/runner commits, pinned images, CI receipts, runtime/schema evidence, verification key and signed manifest form the handoff. Chart values.schema.json locks the exact image/release identities so overlays cannot silently replace the software.

For Argo CD, use the chart repository parent as `repoURL` (without `oci://`), `chart: proposal-studio`, the exact published `targetRevision`, and enable OCI for the registry credential. Argo CD resolves the version tag; ECR immutability and access policies must prevent overwrite/deletion. Verify NCE's recorded chart digest independently before promotion. Helm can pull the recorded digest directly. The destination owns its read-only registry role/token renewal, namespace, secret management, DNS/TLS, policy engine, Argo CD sync and production approvals. No destination deployment is performed by this publisher.

The chart packages web8080, API8000 and isolated browser8010 workloads, service accounts, strict security contexts, probes, resource constraints and NetworkPolicies. API/browser are one replica; API uses one worker and Recreate rollout. It does not invent a distributed worker. External PostgreSQL and S3-compatible storage remain destination bindings. The destination must supply `DATABASE_URL`, `SESSION_SECRET`, authentication/storage settings and approved provider credentials through existing Secrets. It supplies origins, ingress/TLS, image-pull configuration and approved external egress CIDRs separately. The reference-library PVC is optional/read-only. Standard NetworkPolicy does not guarantee FQDN filtering; browser public egress is explicit opt-in and needs destination CNI/proxy review.

For destination workload identity, supply `external.apiServiceAccountAnnotations` and `external.migrationServiceAccountAnnotations` as separate string maps in the destination values overlay (for example, the approved `eks.amazonaws.com/role-arn`). Both maps default to empty; their destination identity bindings are not packaged in the release. API and migration identities may differ: grant each only the external services its runtime needs. Browser and frontend ServiceAccounts cannot receive these annotations. Service-account token automount remains disabled; the destination identity integration must project its own scoped token when required. Identity annotation maps cannot override Helm or Argo CD hook annotations.

For locally enabled application transfer, set the optional
`external.migrationStateClaim` to an existing destination-owned PVC DNS label.
It defaults to empty and creates no PVC. When present, only the API mounts it
read/write at `/migration`, with its existing user/group/fsGroup `10001` and
read-only root filesystem. The browser, frontend and schema migration Job receive
no migration-state mount or receiver private-key files. Retain and back up this
volume: it holds receiver identities, encrypted manifests, replay markers and
transfer evidence required across API replacement/restart. Do not share the claim
with unrelated workloads.

Supply nonsecret `MIGRATION_ENABLED=true` and
`MIGRATION_STATE_PATH=/migration/state/production` through `external.apiEnvironment`.
The same generic map accepts nonsecret customer `AUTH_MODE`, SSO issuer/endpoints
and client ID. Put `MIGRATION_STAGING_DATABASE_URL`, `SSO_CLIENT_SECRET` and other
credential-bearing values in the destination runtime Secret and list their names
in `external.apiEnvFromKeys`; never inline their values in chart settings. The
staging database and any reference/blob custody remain separate reviewed bindings.

These mounts and runtime features require a new NCE-published release containing
the updated chart and application code. An older signed chart cannot gain this
volume through a values overlay, and customer registry relocation is deliberately
limited to image repositories/schema constants and chart version; it cannot add
new templates or features to an old release.

Before rollout, the destination must back up PostgreSQL/object data, drain in-process jobs and verify recovery. An earlier hook creates the migration service account and dedicated egress policy before the Argo PreSync/Helm migration Job. `python -m studio.migrate` runs once before rollout; a PostgreSQL advisory lock serializes this command with API startup. Failed migrations do not retry automatically. Validate recovery and authenticated functionality in the destination before declaring deployment complete.

## Feedback and validation limits

`ci/oci_feedback.py` validates sanitized release-correlated status events; an authenticated, replay-protected persistent feedback endpoint remains a separate integration. NCE owns artifact release state; the destination remains authoritative for deployment/runtime state.

Run `python -m unittest discover -s ci/tests -v` for offline contracts, chart regressions and fail-closed publication checks. Core required CI also runs disposable PostgreSQL application tests, image build and real strict chart validation. `--mode verify` uses synthetic digests and does not build/publish production containers or prove a live destination deployment.

## Customer-owned registry relocation

The original chart pins complete NCE registry image references. A customer mirror
needs a distinct derived chart; changing only a values overlay cannot override
those schema constants. `python -m ci.relocate_release` performs an offline,
deterministic derivation without contacting a registry or cluster. Copy images and
the original chart using the customer's existing approved OCI tools first.

Prepare a private-reviewed JSON image map with exactly the published component
names and customer registry references, each preserving the original SHA-256
digest. Use the receiver's existing owner-only `identity.json` outside Git; never
copy an NCE KMS/private signing key. Review the target origin, new chart version,
publisher SPKI fingerprint, exact release-file SHA-256 and receiver public key.

```sh
python -m ci.relocate_release \
  --release /private/release/release.json \
  --signature /private/release/release.json.sig \
  --publisher-key /private/release/publisher.pub \
  --publisher-spki-sha256 REVIEWED_PUBLISHER_SPKI_SHA256 \
  --release-sha256 REVIEWED_RELEASE_FILE_SHA256 \
  --chart /private/release/original.tgz \
  --image-map /private/approved-mirrors.json \
  --customer-registry registry.customer.example \
  --target-origin https://proposal.customer.example \
  --derived-version 0.1.0-customer.1 \
  --customer-key-file /private/migration/identity.json \
  --customer-public-key REVIEWED_RECEIVER_ED25519_PUBLIC_KEY \
  --output-chart /private/release/customer-derived.tgz \
  --output-receipt /private/release/customer-derivation.json
```

Both output paths must be new. The utility verifies the publisher's detached
P-256 signature and independently pinned public key, exact original release bytes
and chart-package hash. It rejects archive escapes, links, duplicate paths,
devices and oversized packages. It changes only `values.yaml` image repositories,
their corresponding `values.schema.json` constants, `Chart.yaml` version, and an
optional chart-root `release.json` image map. Every image digest, other setting,
template, migration hook, scope and network policy remains unchanged. Package
timestamps and archive ownership are normalized for deterministic output.

The customer signs a domain-separated `chart-relocation` receipt binding the
original publisher/release/chart digests, exact independently reviewed customer
registry/image map, derived package digest/version, target origin and unchanged
file manifest. This receipt is customer derivation evidence, not an NCE publisher
signature over the new chart. Keep both trust chains and original artifacts.

The shared pure `ci/migration_chart.py::verify_derivation` verifier requires the
pinned publisher and customer keys, approved target origin and exact customer
registry/image map. It rebuilds the allowed derivation and compares every output
byte; even a customer-signed change to another chart file is rejected. Destination
acceptance must verify the original OCI closure and this derivation against the
current customer-owned files before activating. Registry copying, customer chart
publication, destination secrets/ingress overlays, Helm validation and actual
rollout remain explicit customer operations; this command does not perform them.
