"""Portable core service client. Configured automatically by marketplace deployments."""
import os
from pathlib import Path
from urllib.parse import urlparse

import requests


class CoreServiceError(Exception):
    pass


class CoreClient:
    def __init__(self, base_url, token, organization_id, allow_local_http=False):
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        if (not parsed.netloc or parsed.username is not None or parsed.query or parsed.fragment or
            parsed.path != "/api/platform/v1" or (parsed.scheme != "https" and not (
                allow_local_http and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("Use the trusted HTTPS core API URL.")
        self.token = token
        self.organization_id = organization_id

    @classmethod
    def from_environment(cls):
        return cls(os.environ["CORE_SERVICES_URL"], Path(os.environ["CORE_WORKLOAD_TOKEN_FILE"]).read_text().strip(),
                   os.environ["CORE_ORGANIZATION_ID"], allow_local_http=os.environ.get("CORE_ALLOW_LOCAL_HTTP") == "1")

    def request(self, method, path, **kwargs):
        # Path components are supplied by these typed methods, never arbitrary remote URLs.
        try:
            result = requests.request(method, self.base_url + path, headers={"Authorization": f"Bearer {self.token}"},
                                      timeout=10, allow_redirects=False, **kwargs)
            if result.status_code not in {200, 201, 202}:
                raise CoreServiceError(f"Core service rejected the request (HTTP {result.status_code}).")
            value = result.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (requests.RequestException, ValueError) as error:
            raise CoreServiceError("Core service request failed.") from error

    @staticmethod
    def slug(value):
        import re
        if not isinstance(value, str) or not re.fullmatch(r"[-a-zA-Z0-9_]+", value):
            raise ValueError("Use a service resource slug.")
        return value

    def discover(self):
        value = self.request("GET", "/integration/")
        if value.get("organization_id") != self.organization_id or value.get("core_services_url") != self.base_url:
            raise CoreServiceError("The integration document does not match this application's organization and core URL.")
        return value

    def ingest(self, pipeline, event_id, data):
        return self.request("POST", f"/pipelines/{self.slug(pipeline)}/events/", json={"event_id": event_id, "data": data})

    def records(self, dataset, after=0, limit=100):
        return self.request("GET", f"/datasets/{self.slug(dataset)}/records/", params={"after": after, "limit": limit})

    def run_workflow(self, workflow, request_id, data):
        return self.request("POST", f"/workflows/{self.slug(workflow)}/runs/", json={"request_id": request_id, "input": data})

    def run_agent(self, agent, request_id, data):
        return self.request("POST", f"/agents/{self.slug(agent)}/runs/", json={"request_id": request_id, "input": data})

    def run_status(self, run_id):
        from uuid import UUID
        return self.request("GET", f"/runs/{UUID(str(run_id))}/")
