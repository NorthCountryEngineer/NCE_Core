"""Minimal marketplace application exercising automatically injected core configuration."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        # Readiness confirms integration files exist, without returning credentials.
        configured = all(os.environ.get(name) for name in ["CORE_ORGANIZATION_ID", "CORE_SERVICE_ID", "CORE_ISSUER", "CORE_SERVICES_URL"])
        readable = all(Path(os.environ.get(name, "/missing")).is_file() for name in ["CORE_WORKLOAD_TOKEN_FILE", "CORE_OIDC_CLIENT_SECRET_FILE"])
        value = json.dumps({"status": "ok" if configured and readable else "unconfigured",
                            "organization_id": os.environ.get("CORE_ORGANIZATION_ID"), "integrated": configured and readable}).encode()
        self.send_response(200 if configured and readable else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        self.wfile.write(value)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
