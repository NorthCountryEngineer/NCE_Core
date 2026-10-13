AWS deployment sources only; nothing in this directory provisions infrastructure
when imported or rendered. The detailed two-site runbook is in the Axolotl News
project's [AWS runbook](https://github.com/NorthCountryEngineer/Axolotl_News/blob/main/docs/AWS_DEPLOYMENT.md).

For an existing installation, the [app-branded authentication upgrade](https://github.com/NorthCountryEngineer/Axolotl_News/blob/main/docs/APP_AUTH_DEPLOYMENT.md) preserves the stack parameters and keys, publishes a new image, runs the additive branding migration with the service stopped, then restarts it before releasing Axolotl's frontend. The Core Amplify proxy needs no rebuild for this feature.

`core-registry.template.json` creates an ECR repository. `core-fargate.template.json`
creates an HTTPS ALB, one ARM64 Fargate service containing Django and its persisted
worker, and private PostgreSQL RDS. The initial service count is zero so migrations
and the first administrator can be created before the service starts. Both templates
create resources that can incur AWS charges when deployed. Zero service tasks does
not stop ALB, RDS, Secrets Manager, log retention or public IPv4 charges.

Core's frontend is the Node SSR proxy in `deployment/amplify-core`, using the root
`amplify.yml` and the Amplify compute IAM role output by the Core template. Core's
permanent public issuer is the Amplify frontend origin. The HTTPS ALB origin uses
`core-origin.axolotl.news`, a same-region ACM certificate, and a DNS-only Cloudflare
CNAME. Set Amplify's cookie cache mode to `AMPLIFY_MANAGED`; private responses also
carry `Cache-Control: private, no-store`. Amplify's SSR compute role can read only the
separate origin-header secret. It cannot read Django's encryption/signing key or
Core's OIDC RSA private key.

The low-capacity defaults use one .25 vCPU / 1 GiB task and one `db.t4g.micro` RDS
instance. Web and worker share the task. Public-subnet tasks avoid a NAT gateway
and accept inbound traffic only from the ALB. Single-instance RDS and task upgrades
that stop the previous task before starting the next trade availability for lower
cost. Monitor workload before increasing capacity. Pricing and regional availability
must be checked before deployment; this is not a free-hosting guarantee.

Generate new runtime keys only for a NEW installation:

```sh
.venv/bin/python infra/aws/generate-runtime-secret.py --output /private/path/core-runtime.json
```

Store this JSON in Secrets Manager and keep a private backup. Its Django key also
protects stored integration credentials; preserve it and the OIDC key across releases.
The bootstrap password is delivered only to the dedicated first-admin task. The
normal service task role has no AWS application permissions. The dedicated external
registration task can write only one configured SSM SecureString parameter, with
`Overwrite=False`; existing credentials are never replaced by a retry.

The release helper starts billable compute and refuses to run unless
`CORE_ENABLE_AWS_DEPLOYMENT=true` is explicitly set. Run migrations once per release,
then bootstrap once, then start the service. Registration runs with `--registration`.
No secret is passed as a command-line argument or printed in release logs.

Safe local verification:

```sh
.venv/bin/python infra/aws/render-template.py
.venv/bin/python manage.py --local test core.tests.test_aws
cd deployment/amplify-core
npm ci --offline --ignore-scripts --no-audit --no-fund
npm test
```

The image build downloads the official RDS CA bundle at build time and verifies
PostgreSQL TLS using `sslmode=verify-full`. Builds, pushes and deployments have not
been performed. A real AWS acceptance check must validate the issued certificate,
proxy role, login/CSRF session flow, token exchange, origin denial and worker/database
readiness before inviting users.
