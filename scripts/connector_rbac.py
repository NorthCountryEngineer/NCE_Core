"""Render namespace-scoped connector RBAC. Apply with an existing cluster operator account."""
import argparse
import json
import re

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--namespace", required=True)
args = parser.parse_args()
if len(args.namespace) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", args.namespace):
    parser.error("Use a Kubernetes DNS label.")
meta = {"name": "core-runtime", "namespace": args.namespace}
resources = [
    {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta},
    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta, "rules": [
        {"apiGroups": [""], "resources": ["secrets", "services"], "verbs": ["get", "list", "watch", "create", "update", "patch"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "watch", "create", "update", "patch"]},
        {"apiGroups": ["apps"], "resources": ["deployments/scale"], "verbs": ["get", "update", "patch"]},
        {"apiGroups": ["networking.k8s.io"], "resources": ["ingresses"], "verbs": ["get", "list", "watch", "create", "update", "patch"]},
    ]},
    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta,
     "subjects": [{"kind": "ServiceAccount", "name": "core-runtime", "namespace": args.namespace}],
     "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "core-runtime"}},
]
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}))
