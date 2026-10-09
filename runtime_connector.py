"""Outbound-only Kubernetes connector for an existing cluster.

Requires kubectl and namespace-scoped credentials, plus requests. No Django dependency.
Control-plane credentials stay in a local secret file. Kubernetes applies receive
integration secrets over stdin, never command-line arguments or diagnostic logs.
"""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


class ReconciliationError(Exception):
    pass


class KubernetesAdapter:
    def __init__(self, namespace, context=None, kubectl="kubectl"):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", namespace) or len(namespace) > 63:
            raise ValueError("Invalid namespace.")
        self.namespace = namespace
        self.command = [kubectl] + (["--context", context] if context else []) + ["--namespace", namespace]

    def run(self, arguments, value=None, allow_missing=False):
        try:
            result = subprocess.run(self.command + arguments, input=json.dumps(value) if value is not None else None,
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ReconciliationError("Kubernetes command failed.") from error
        if result.returncode:
            raise ReconciliationError("Kubernetes rejected the requested operation.")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            return {}

    def reconcile(self, spec):
        try:
            if spec["namespace"] != self.namespace:
                raise ReconciliationError("Namespace mismatch.")
            name = spec["resource_name"]
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name):
                raise ReconciliationError("Invalid resource name.")
            organization = spec["organization_id"]
            existing = self.run(["get", "deployment", name, "--ignore-not-found", "-o", "json"])
            if existing and existing.get("metadata", {}).get("labels", {}).get("core.platform/organization") != organization:
                raise ReconciliationError("Resource ownership mismatch.")
            if spec["mode"] == "controller":
                if not existing:
                    raise ReconciliationError("The controller deployment does not exist.")
                annotation = existing.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {}).get("core.platform/revision", "0")
                if spec.get("action") == "restart" and int(annotation) < spec["revision"]:
                    self.run(["patch", "deployment", name, "--type", "merge", "--patch-file", "/dev/stdin"],
                        {"spec": {"template": {"metadata": {"annotations": {"core.platform/revision": str(spec["revision"])}}}}})
            elif spec["desired_state"] == "stopped":
                if existing:
                    self.run(["scale", "deployment", name, "--replicas=0"])
                    observed = self.run(["get", "deployment", name, "-o", "json"])
                    if observed.get("status", {}).get("replicas", 0) > 0:
                        return self.report(spec, "pending", False)
                return self.report(spec, "stopped", False)
            else:
                # The authenticated control plane sends only these approved object kinds.
                for item in spec["manifests"]:
                    if (item["kind"] not in {"Secret", "Deployment", "Service", "Ingress"} or
                            item["metadata"]["namespace"] != self.namespace or item["metadata"]["name"] != name or
                            item["metadata"].get("labels", {}).get("core.platform/organization") != organization):
                        raise ReconciliationError("Manifest scope mismatch.")
                    # Protect all object kinds, including integration Secrets, from takeover.
                    current = self.run(["get", item["kind"].lower(), name, "--ignore-not-found", "-o", "json"])
                    if current and current.get("metadata", {}).get("labels", {}).get("core.platform/organization") != organization:
                        raise ReconciliationError("Resource ownership mismatch.")
                self.run(["apply", "--server-side", "--field-manager=core-platform", "-f", "-"],
                         {"apiVersion": "v1", "kind": "List", "items": spec["manifests"]})
            observed = self.run(["get", "deployment", name, "-o", "json"])
            state = observed.get("status", {})
            generation = observed.get("metadata", {}).get("generation", 0)
            replicas = observed.get("spec", {}).get("replicas", 1)
            healthy = bool(replicas > 0 and state.get("observedGeneration", -1) >= generation and
                           state.get("updatedReplicas", 0) == replicas and state.get("replicas", 0) == replicas and
                           state.get("readyReplicas", 0) >= replicas and state.get("availableReplicas", 0) >= replicas)
            return self.report(spec, "running" if healthy else "pending", healthy)
        except (ReconciliationError, KeyError, TypeError, ValueError):
            return self.report(spec, "failed", False)

    @staticmethod
    def report(spec, state, healthy):
        return {"deployment_id": spec["deployment_id"], "revision": spec["revision"], "state": state, "healthy": healthy}


class RuntimeConnector:
    def __init__(self, core_url, token, adapter, allow_local_http=False):
        parsed = urlparse(core_url)
        if (not parsed.netloc or parsed.username is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or
            (parsed.scheme != "https" and not (allow_local_http and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("Use the trusted HTTPS core origin.")
        self.core_url, self.token, self.adapter = core_url.rstrip("/"), token, adapter
        self.reports = []

    def tick(self):
        result = requests.post(self.core_url + "/api/platform/v1/runtime/sync/", json={"reports": self.reports},
            headers={"Authorization": f"Bearer {self.token}"}, timeout=15, allow_redirects=False)
        if result.status_code != 200:
            raise ReconciliationError("The control plane rejected runtime authentication or synchronization.")
        value = result.json()
        if value.get("version") != 1 or value.get("namespace") != self.adapter.namespace:
            raise ReconciliationError("Runtime namespace or protocol mismatch.")
        self.reports = [self.adapter.reconcile(spec) for spec in value["deployments"]]

    def run(self, once=False, interval=15):
        while True:
            try:
                self.tick()
                if once:
                    # A second sync delivers the observations from this pass.
                    self.tick()
                    return
            except (requests.RequestException, ReconciliationError, ValueError):
                if once:
                    raise ReconciliationError("Runtime synchronization failed.") from None
                print("Core synchronization unavailable; retaining existing workloads and retrying.", flush=True)
            time.sleep(max(1, min(interval, 30)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--context")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--allow-local-http", action="store_true")
    args = parser.parse_args()
    adapter = KubernetesAdapter(args.namespace, args.context)
    RuntimeConnector(args.core_url, Path(args.token_file).read_text().strip(), adapter, args.allow_local_http).run(args.once)


if __name__ == "__main__":
    main()
