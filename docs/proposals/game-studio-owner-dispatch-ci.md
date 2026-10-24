# Approved Game Studio owner-dispatch CI evidence policy

Status: **owner approved at `2026-10-24T14:42:20Z`; activated in Core source**. The owner explicitly requested: “Enable this Game Studio policy.” This scoped source/test/documentation change is based on corrected Core release `52d8ad1c3846a9151b482c499576e5fe964290ec`, which preserves the production purpose-code UI removal from `c27e6a03ff47adf4acc8e670ec2f240127744172`. Publishing this change does not by itself update a running Core service. Normal Core CI and deployment acceptance remain required.

## Concrete policy

All existing push-event policies remain in force, including Game Studio's normal push path. A failed, queued, malformed, inaccessible, truncated or ambiguous push result cannot use an alternate event. Only a repeatedly complete, honest empty push listing can reach this additional Game Studio policy.

| Binding | Approved value |
| --- | --- |
| Repository | `NorthCountryEngineer/Game_Studio` |
| Repository numeric ID | `1432213176` |
| Repository owner | GitHub User ID `132168834` |
| Workflow | `.github/workflows/ci.yml`, numeric ID `395127780` |
| Event | `workflow_dispatch` |
| Default branch | `main` |
| Requested deployment source | The exact 40-character commit, still current `refs/heads/main` |
| Reviewed workflow SHA256 | `cdcec9d0a51f3b37012e6b7f757c6d940ae808d010567b2668d4565fc6f22998` |
| Workflow review source | `60af8b3c19727f257f91ef935bd99e9cdafdbe6f`; bytes copied to `ci/fixtures/game-studio-dispatch-ci.yml` |
| Required job | Exactly one `Game Studio required checks` job |
| Check producer | GitHub Actions app ID `15368`, slug `github-actions`, GitHub owner ID `9919` |
| Approval cutoff | `2026-10-24T14:42:20Z` |

Both `actor` and `triggering_actor` must be that exact owner User ID. This excludes another collaborator, bot or rerun performed by a different account. The owner/repository IDs are verified by read-only GitHub API metadata; display names do not confer authority.

The trusted release operator recorded the actual approval's UTC time in the reviewed Core source field `approved_after`. A **new** Game Studio workflow dispatch is required with `created_at` strictly after that cutoff. Rerunning a run created before approval cannot qualify. No unverified `13:46` time is encoded. No environment variable, application manifest, browser input, GitHub token or stored prior success can change this policy.

The latest attempt for the exact source must be complete/successful; a newer failed, queued, skipped, cancelled, foreign-actor or otherwise invalid run blocks an older success. A complete bounded listing is required, with no duplicate run IDs or crossed repository/workflow identities. Foreign forks, events, paths and numeric repository IDs are rejected.

The workflow bytes at the immutable source commit must match the exact reviewed SHA256. The reviewed required job and its checkout are unconditional; its checkout, setup and all verification/build commands must appear exactly once as successful completed steps. A missing or skipped required step fails even if a job reports success. Optional GitHub runner cleanup may be skipped; it cannot replace a required check. The attempt-specific job and GitHub Actions check run must agree on source SHA, job ID, status and check-suite identity.

The gate rereads the dispatch index, current main reference, repository default branch/owner and push index before releasing a receipt. A changed attempt, owner, default branch or main head fails. If push evidence appears during verification, the dispatch path refuses and requires the normal push policy. Receipts record the alternate event, workflow digest, pinned owner and approval cutoff for review.

The existing shared 20-request and 40-second decision allowance remains unchanged. The successful owner-dispatch path plus a normal Core push verification uses 20 reads. Additional lagging indexes or slow responses may exhaust that allowance and are refused. There is no token fallback, CI invocation, cancellation, IAM mutation or deployment in the gate.

## Approval scope

The owner approved the source-controlled approval timestamp followed by a new owner dispatch. The timestamp was measured and recorded after that explicit approval. The policy remains restricted to this exact repository, owner, main branch, workflow bytes and required checks; changing any pin requires another reviewed policy change. If the timestamp is unset or invalid, dispatch admission fails closed.

An even narrower alternative could pin one deployment commit and dispatch run ID, with a one-release expiry. That would require another deliberate policy change for later Game Studio releases. It is not implemented here because the owner approved a repository-specific ongoing CI evidence policy. The separate normal AWS deployment approval was not treated as approval of this policy.

The workflow digest pins the reviewed YAML, including `actions/checkout@v4` and the existing setup action tags; it does not independently make those upstream tags immutable. Pinning their action commit SHAs would be a separate Game Studio workflow edit, followed by a new reviewed workflow digest and fresh CI run. This policy does not silently rewrite the application workflow.

## Local verification and limits

Run from this checkout:

```sh
python3 -m unittest ci.tests.test_github_gate ci.tests.test_owner_dispatch_proposal -v
```

The original 35 gate tests continue to pass. New tests use only synthetic repository/run/check data and the reviewed public workflow fixture. They cover inactive defaults, honest-empty push fallback, exact repository/owner/workflow/main bindings, post-approval creation, workflow-byte substitution, both actor identities, all required checks, forged check providers, latest/rerun failures, concurrency races and unchanged shared request limits. The inactive configuration is explicitly tested by replacing the approved timestamp with `None`; dispatch admission then fails closed. The measured production cutoff also has a dedicated regression test.

Read-only GitHub inspection confirmed the fixed repository, owner, active workflow, reviewed workflow digest and official GitHub Actions check producer. That inspection did not itself authorize activation, change any GitHub setting, dispatch CI or establish a production policy release. The owner subsequently approved this exact policy. A reviewed Core image and normal CI/release acceptance are still required before relying on it in production.
