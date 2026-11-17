# NCE Robotics edge applications

NCE Robotics adds an edge-device runtime beside Core's existing Kubernetes and AWS deployment paths. A registered robot runs an outbound HTTPS agent and keeps its local application running when Core is unavailable. Native iOS clients use the same Core organization, service permissions, OAuth PKCE, and live authorization checks as other applications.

## Operator setup

Apply migrations, then set `CORE_EDGE_ARTIFACT_ROOT` to a private persistent directory shared by the Core web and worker processes. Production artifact operations require explicit configuration. Keep the directory outside static/public hosting and include it in backup and restore procedures.

```sh
python manage.py migrate --noinput
python manage.py register_robotics --organization EXISTING_ORGANIZATION_SLUG
```

Registration creates the `nce-robotics` installation and its public native OAuth client. It prints the public client ID and exact HTTPS callback. It does not create an Apple signing identity, purchase infrastructure, open router ports, or enroll a device.

The organization administration dashboard links to **Robotics** at:

```text
/organizations/<organization-slug>/platform/robotics/
```

The application uses the existing Core service-role editor. Permissions are `device-read`, `device-manage`, `release-manage`, `software-deploy`, `os-manage`, and `job-approve`. Existing organization administrators receive the ordinary administrator behavior; other members require explicit role assignments. Generic AWS external-app account-role presets do not back this runtime.

Create an enrollment code for the named device. The code expires after ten minutes, is consumed once, and is exchanged by the device for a credential scoped to that device. Core stores only credential/code hashes. Revocation denies the next authenticated device operation. Device credentials expire after 90 days; this version requires re-enrollment rather than automatic rotation.

### Activate on the existing Lightsail Core host

Use the operator release path in [the Lightsail operating guide](lightsail-deployment.md). Core itself is outside the Proposal Studio/Academy release mappings. The generic Kubernetes pipeline and proposal-specific OCI workflow are not substitutes for this Core host release.

1. Finish the required checks, commit the reviewed source, and build `Dockerfile.lightsail` for the existing host architecture with `SOURCE_COMMIT` set to that exact 40-character commit. Pin the resulting immutable image for both `core-web` and `core-worker`; preserve the current issuer, databases, keys, bindings, and other application services.
2. Have the operator provision a private persistent artifact directory and mount the same directory into Core web and worker. Set `CORE_EDGE_ARTIFACT_ROOT` to its container path in both services. The directory must be writable by Core's UID/GID **10001** and must not be served by the proxy or static-file configuration. Include its contents in backups alongside the database records that reference them.
3. Take the existing release backups and retain the previous image/configuration. Using the normal Core image's `manage` launcher and preserved runtime configuration, run migrations once before starting the new processes. Do not put migrations into normal web or worker startup. Then recreate only the two Core services using the reviewed image and mounts.
4. Check `check --deploy`, `migrate --check`, `/health/`, discovery/JWKS, and the existing authenticated Core/application flows. Confirm the new worker is running, the artifact path is writable, and the Robotics administration page is available. Run `register_robotics --organization` with the **actual existing organization slug** through that same management launcher; capture its public client ID and assign any non-administrator roles explicitly.
5. Create a fresh enrollment code in Core and install/enroll the agent on the intended Pi. Verify its heartbeat and a status refresh before approving software deployment or OS maintenance. Signed iOS activation additionally needs the actual Apple Team ID/bundle configuration and a successful HTTPS callback association.

An image rollback does not undo jobs already delivered to a device or restore its OS. Stop new management work and reconcile any running/uncertain device jobs before an operator rollback; do not replay them. Preserve artifact files and the new database records during ordinary image rollback. Database restoration is a separate coordinated recovery operation under the existing backup procedure.

## Device operations

| Operation | Permission | Review |
| --- | --- | --- |
| `status.refresh` | `device-read` | Queued immediately |
| `app.deploy`, `app.rollback` | `software-deploy` | Separate `job-approve` approval, with operation permission |
| `os.update.check` | `os-manage` | Queued immediately; refreshes package metadata and returns a plan |
| `os.update.apply`, `os.reboot` | `os-manage` | Separate `job-approve` approval, with operation permission |

Application releases are immutable tar.gz artifacts, restricted to the companion runtime's `nce-release.json` and `app/` files. Core validates size, paths, member types and manifest before storing a SHA-256 digest. The device authenticates the download, verifies the digest, and validates the archive again. A failed application health check restores the prior release.

Updates apply only an exact recently checked package plan; no distribution upgrade, package removal, or generic shell command is offered. Job requests, approvals, claims, starts, results and revocations are audited. Core rechecks current requester and approver permissions before execution. Canceling an already-started package transaction cannot safely undo it.

Expired/uncertain mutating jobs are not automatically executed again. The agent's durable journal retries result delivery without repeating the side effect. Reboot jobs acknowledge scheduling before reboot and complete after observing a new boot ID. If a device vanishes, the worker marks orphaned work uncertain for operator review.

## Native iOS configuration

The native app uses an exact HTTPS callback at `/apps/nce-robotics/oauth/callback`, with public-client authorization code + PKCE. Configure `CORE_IOS_TEAM_ID` and `CORE_IOS_BUNDLE_ID` to match the signed app. Without a valid configuration, the association endpoint returns 404 instead of publishing a fabricated association.

The `/.well-known/apple-app-site-association` response uses the exact app ID for `applinks` and `webcredentials`. The latter is required by `ASWebAuthenticationSession` HTTPS callback handling; the app does not call shared-web-credential APIs. No wildcard app association, general custom-scheme exception, confidential client secret, root password or SSH key is added to the iOS app.

Core manages authorization and device jobs. Building and installing the native iOS binary still requires Xcode and Apple-supported signing/distribution. The source project's build scripts distinguish Foundation checks from an actual iOS build.

### Free Apple Personal Team local testing

The companion iOS project's **PersonalTeam** configuration uses a separate bundle, `ai.nnytech.NCERobotics.Development`, and the exact private-use callback `ai.nnytech.ncerobotics.dev:/oauth/callback`. It does not require the Associated Domains entitlement or the production AASA association. Core's authorization and token endpoints still use the configured HTTPS issuer.

After deploying this Core version and applying its migrations, an organization administrator can select **Register Personal Team development client** in the Robotics page. The page then shows a distinct **Development client ID**. The equivalent operator command is:

```sh
python manage.py register_robotics --organization EXISTING_ORGANIZATION_SLUG --development-client
```

Copy that public ID into the development build's `NCE_PERSONAL_TEAM_CLIENT_ID`. Keep the production client ID in the production build configuration. The command creates or reuses only the marked development registration; it does not convert the HTTPS client, change its callback, or grant users additional roles.

The private-use callback is allowed only for this explicit public-client registration bound to the active `nce-robotics` installation. Other applications, different schemes/paths/query strings, and confidential clients cannot use the exception. Generic registration forms cannot enable it. S256 PKCE, unpredictable state, explicit consent, tenant membership, service permissions, and live revocation remain enforced. Personal Team signing and device-installation limits still apply; this is a local testing route, not App Store distribution.

## API and validation

Versioned APIs are under `/api/edge/v1/`. Human requests use a current service-bound Core OAuth bearer. Agent routes use a separate per-device bearer and cannot become a human session. Artifact downloads require a current job lease and are limited to that device's approved release.

```sh
python manage.py --local test platform_services.tests.test_edge --noinput
python manage.py --local makemigrations --check --dry-run
```

The companion project also runs the actual Core HTTP contract through the agent and a temporary helper, including deployment/rollback, lost acknowledgements, reboot recovery, and revocation. Live Pi installation, a production Core rollout, and a signed iPhone build are separate acceptance steps.

## Live command console

Agent 0.2.0 advertises `app.command` in its report. Authorized device readers can submit a bounded command string with a UUID request ID through the existing jobs API, the native Console, or the CSRF-protected device console linked from the robotics dashboard. Command requests expire after two minutes and require current authority again at claim and start. Older agents do not receive command jobs.

The agent uses a fixed loopback application endpoint as an unprivileged user; it never forwards these commands to the root helper. The first application supports machine diagnostics and literal echo receipts. Its output includes the actual hostname, observation timestamp, application version, process ID, and matching command/job UUID. Unknown commands and unavailable readings return explicit device errors. Core records returned results; it does not generate replacement responses.

Watch the device with `journalctl -fu nce-robotics.service -o cat` over SSH. Application command receipts survive restarts and reject reuse of an ID with different input. The console does not install an AI model or operate actuators.
