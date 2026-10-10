# Kubernetes modernization and segregated environments

Core Platform adopts existing Kubernetes Deployments into an organization's
private marketplace catalog. Administrators approve consumption of identity,
chat, data, agents, workflows, training, and fabric capabilities. The namespace
connector generates integration configuration and reports health, wherever the
existing cluster is deployed. No cluster, paid service, or model provider is
provisioned by this process.

## Register environments

From the organization's **Central admin → Sandbox, development, and production**,
create `sandbox`, `development`, and `production` with distinct namespaces, for
example `customer-sandbox`, `customer-dev`, and `customer-prod`. Staging and UAT
remain available. Production requires an HTTPS application origin. Each record
has its own origin, ingress/TLS defaults, boundary tag, active flag, automatic-deployment preference, and runtime
accounts. Identity and namespace are immutable once created. Automatic deployment is approved per environment; a sandbox preference does
not enable production deployment. Changing an origin
changes defaults for future deployments; existing callbacks remain explicit.

Use one connector Kubernetes ServiceAccount and one Core runtime token per
environment namespace. Register these through **Connect Kubernetes** and select
the environment. Tokens are shown once and stored only as hashes in Core.
Deployment and integration keys are also bound to the selected environment;
installing the same marketplace application does not share keys between its
instances. Administrators can manage all environments centrally.

Assign the environment's boundary tag to each dataset, conversation, workflow,
agent definition, training scenario, fabric agreement, or registered application
resource. Pipelines and scenarios inherit their dataset's boundary. Environment
keys cannot access unassigned resources, another environment, or a chain that
crosses environments. Additional classification, purpose, MFA, role and tag
controls remain enforced. An application cannot exchange another environment's
agent credentials or change workflow environment context. Human users need
environment-tag grants through their memberships or roles; organization
administrators retain central control. Disabling an environment revokes live
service access without deleting its applications or data.

## Prepare namespace isolation

Render manifests locally, using the real organization UUID, namespace, and
Core endpoint IP. These commands only write local configuration:

```bash
uv run python scripts/environment_bundle.py \
  --namespace customer-sandbox --environment sandbox \
  --organization-id YOUR_ORGANIZATION_UUID \
  --core-cidr YOUR_CORE_ENDPOINT_IP/32 > sandbox-isolation.json
uv run python scripts/connector_rbac.py \
  --namespace customer-sandbox > sandbox-connector-rbac.json
```

Use `/128` for IPv6. Repeat with separate namespaces for development and
production. Review the rendered files and apply them using your existing cluster
administrator account when ready. Applying a bundle to an existing namespace
affects its networking and Pod Security admission; assess its workloads first.
The connector itself has no permission to create namespaces or change network
policies. Its cluster-scoped permission is **get on its one named namespace**;
workload and Secret operations remain namespace-scoped.

The bundle labels the namespace with organization/environment identity, enables
restricted Pod Security admission, denies ingress and egress by default, and
allows only same-namespace traffic, Core DNS, the designated ingress-controller
namespace, and TCP to the explicit Core endpoint IP/port. The default ingress
namespace is `ingress-nginx`; override with `--ingress-namespace`. The default
Core port is 443; override with `--core-port`. Configure additional allow rules
explicitly for an application's required database or external dependencies.

**NetworkPolicy enforcement requires a compatible CNI**, such as Calico or
Cilium. Kubernetes namespace separation alone cannot block network traffic.
Review other policies because their allowances are additive; an existing
allow-all policy defeats isolation. Account for DNS labels, node-local DNS,
load-balancer endpoint changes and your CNI's Service NAT behavior. Prefer
dedicated clusters and separate Core/database deployments where physical
production isolation is required. Namespace policies are not a compliance
certification. The connector checks identity labels and the default-deny policy
before Core sends environment deployment configuration; it cannot prove that
the cluster's CNI enforces it.

## Adopt an existing application

1. Keep the environment connector running with its private runtime-token file,
   selected Kubernetes context and namespace. Use the ServiceAccount credentials
   from the rendered RBAC, rather than a cluster administrator kubeconfig.
2. Open **Central admin → Modernize a Kubernetes application**. The connector
   discovers up to 100 Deployments per namespace and reports only names, UIDs,
   and container names/images. Secret contents and application environment
   variables are never stored in discovery inventory. Inventory expires after
   90 seconds; reconnect before approving a stale entry.
3. Select the existing Deployment and consuming container, supply its display
   name/reference, existing application origin and OIDC callback path, and
   approve only the Core capabilities it needs. Confirm the integration rollout.
4. Core creates a private marketplace entry and installation, an environment
   deployment record, an OIDC application, and an independent workload key. The
   next connector poll mounts a new integration Secret read-only and injects
   `CORE_ISSUER`, organization/service/deployment UUIDs, `CORE_ENVIRONMENT`, OIDC
   client settings and Core service discovery configuration into that container.
5. Review the environment label and reported readiness in **Deployed instances**.
   Verify a same-environment pipeline/chat request and a denied production
   request using a sandbox key. Configure the application's OIDC framework and
   callback handler, and register business permissions/resources in Core.

Adoption preserves images (including owner-managed image tags), replica counts,
selectors, sidecars, application environment variables, probes, resources,
storage, Services and Ingress. It uses UID and resourceVersion preconditions to
reject replacement/racing workloads. Foreign ownership, conflicting `CORE_`
settings, mount collisions, privileged containers, host namespaces and hostPath
volumes fail before workload mutation. Integration uses its own Secret and
mount; Kubernetes workload credentials remain on the connector host.

**Disable integration** revokes its workload credential and OIDC access. It does
not scale down the owner's application. **Enable integration** rotates the
workload key and refreshes the mounted configuration. Releases and restarts
remain with the application owner's CI/CD pipeline. Owner controllers must
preserve Core's integration labels and pod configuration; coordinate Helm/GitOps
ownership before adoption.

The first adapter covers `apps/v1 Deployment`. StatefulSets, Jobs, operators and
custom resources need a matching adapter. Kubernetes compatibility supplies
deployment/integration configuration; arbitrary application code still needs
to consume OIDC and the Core APIs. Core does not invent an application's business
permissions or transform its architecture automatically.

## Consume the generated interfaces

For Python applications, import or package the portable clients with `requests`:

```python
from core_client import CoreClient
from identity_client import ResourceClient

services = CoreClient.from_environment()
integration = services.discover()  # verifies organization, Core URL and environment
records = services.records("sandbox-records")

authorization = ResourceClient.from_environment()
# The incoming user's access token comes from the application's OIDC login.
claims = authorization.require(user_access_token, "records-read",
                               resource_type="record", resource_id=record_id)
```

Register `records-read` and the selected resource in Core before authorizing it.
Other languages can consume the same OIDC discovery and HTTP APIs. Backend
client secrets remain in mounted files; do not expose them in browser code.
Human token introspection and agent authorization recheck live environment,
membership, roles, task scope, resource tags and revocation. OAuth clients for
two environments cannot introspect each other's tokens even when they use the
same service UUID.

## Upgrade existing Core installations

Run database migrations before restarting web and worker processes. Existing
organization deployment configuration becomes its initial environment; runtime
nodes, monitored instances and generated deployment keys retain their UUIDs and
are assigned to that environment. Existing manual keys remain stored but are
denied once the organization has registered environments. Revoke those keys and
create new environment-scoped keys through the panel. Assign environment tags
to existing business resources before applications resume consuming them.

Update namespace labels, isolation rules, connector RBAC and connector code
before reconnecting an existing runtime. No migration modifies a Kubernetes
cluster or deletes customer data. Use separate CI environment configuration
files, kubeconfigs, runtime/database secrets and release approvals for sandbox,
development and production; generated CI manifests carry the environment label
and `CORE_ENVIRONMENT` as well.

```bash
CORE_DEBUG=1 uv run python manage.py test platform_services.tests.test_modernization
```

These tests cover environment isolation, identity revocation, agent exchange,
chat assignment, adoption ownership/preconditions and configuration preservation.
Live cluster/CNI verification remains an operator acceptance check.
