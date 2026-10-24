# Approved policy: one Game Studio release verified in Core CI

**Owner approved at `2026-10-24T15:06:30Z`; registered workflow pinned in Core source.** The owner explicitly approved Core verification for this release. This scoped change starts at corrected Core `285cf464606d456a2b8d3be5013f7e1eaf30b454`; the production purpose-code UI removal is preserved. The source approval cutoff and 24-hour expiry are recorded. Registration commit `65e24ba4375f17faa856aa883e52bbb806dc6398` published the workflow while admission was still denied; GitHub assigned workflow ID `395195149`. The activation source now pins that reviewed ID. Unset, expired or invalid policy fields continue to fail closed. Publication does not deploy Core, change AWS resources or change GitHub settings.

The proposed public workflow checks out only `NorthCountryEngineer/Game_Studio` commit `55450955f8acfe88171289ec262b15f17e1c5e84`. It runs all thirteen previously reviewed checkout/setup/verification/build steps in an unconditional GitHub-hosted job, with `contents: read`, `persist-credentials: false`, no workflow inputs, no repository secrets, no cloud credentials, no paid providers and no caches. Its only event is a push to `codex/game-studio-onboarding` in `NorthCountryEngineer/NCE_Core`. The gate requires both the actor and triggering actor to be the pinned owner. GitHub supplies its ordinary job-scoped read-only Actions credential; this is not a claim that Actions runs without any platform token.

The special execution path admits only the exact Game Studio commit above. The broker run, job, check and workflow blob must match the deployment's exact trusted **Core runner commit** on the fixed branch; the byte-pinned literal checkout binds the different **Game Studio application commit**. Core's normal successful push checks are independently mandatory for that same runner commit. Core's existing default branch remains `main`; it is not changed or required to equal the release branch.

| Binding | Value |
| --- | --- |
| Game Studio repository / numeric ID | `NorthCountryEngineer/Game_Studio` / `1432213176` |
| Core execution repository / numeric ID | `NorthCountryEngineer/NCE_Core` / `1412449668` |
| Owner, both actor identities | GitHub User `132168834` |
| Execution workflow | `.github/workflows/game-studio-release-checks.yml` |
| Required broker job | `Game Studio one-release required checks` |
| Workflow SHA256 | `3d8e4a2dc12bff65407900b63c5ebe82d8300af7215c13341018c32aebaab44d` |
| Numeric workflow ID | `395195149` |
| New owner approval cutoff | `2026-10-24T15:06:30Z` |
| Expiry | `2026-10-25T15:06:30Z` |

The owner separately approved this repository change, exact app commit, scoped evidence policy and bounded window; the earlier Game Studio-native approval was not reused. The workflow was first published with its numeric ID unset; the assigned ID was read from GitHub, reviewed against the exact path/state and pinned in the subsequent activation commit. Only a new owner push/run created strictly after approval can qualify. The final activation commit must pass both the broker checks and normal Core checks. A pinned numeric ID is retained rather than accepting a newly discovered workflow identity dynamically. A push event avoids the requirement that a workflow_dispatch file already exist on Core's older default branch.

Before broker checks are considered, a complete exact-application native CI listing must contain no push run and no successful, failed, cancelled, skipped, started or foreign native attempt. It may be honestly empty or contain at most two valid owner-created, queued workflow-dispatch attempts, each with **exactly zero jobs**. The gate checks all rows, not only a selected success. A failed push or native attempt is never bypassed. If native push CI becomes available, use the existing normal policy. The full native index, zero-job attempts, application main head and repository owner are fetched again before admission; failure or started jobs appearing during verification denies the alternative.

The existing exact-workflow machinery still enforces the successful latest broker attempt, both owner actors, exact branch/head/source, reviewed workflow bytes, all thirteen required successful steps, attempt-specific jobs, numeric repository/workflow identities and the official GitHub Actions check producer. Broker attempt, Core branch and repository metadata are freshly reread. Native application evidence is rechecked once more after Core CI proof, then expiry is rechecked before returning.

The special proposal has a **32-request, 40-second** allowance for the complete app eligibility + broker proof + normal Core push proof. The verified one-orphan case uses 26 requests. Existing policies retain their unchanged **20-request, 40-second** limits. The larger broker-only allowance is an explicit part of this new approval proposal; normal failures never select it, and a different application commit cannot select it.

“One release” means one fixed source revision during a maximum 24-hour window. The gate is read-only and cannot consume durable single-use authorization, so repeated deployment attempts for that same source during the window remain possible. Truly single-use authorization would require a separate durable deployment-ID consumption record. This proposal grants no IAM, model, provider or application permissions and changes no deployment templates.

The application checks retain their existing tagged setup actions; the reviewed workflow digest does not independently make upstream action tags immutable. The workflow has no conditional checks and no `continue-on-error`. Repository settings are not changed, and GitHub's orphaned-run issue is not claimed resolved.

Offline validation:

```sh
python3 -m unittest ci.tests.test_github_gate ci.tests.test_owner_dispatch_proposal ci.tests.test_game_studio_broker_proposal -v
```

Tests cover inactive defaults, exact app and Core bindings, literal checkout/permissions, owner and official check identity, missing/skipped steps, malformed/failed/push native evidence, queued attempts gaining jobs, application main/owner races, latest broker reruns, Core CI failure, short expiry, and unchanged normal policy bounds. The 62 offline tests pass. Live workflow and Core deployment acceptance remain separate required steps; this document does not claim that publication has deployed a service.
