"""Strict, untrusted external Kubernetes deployment feedback contract.

This is validation and correlation only. An authenticated ingest endpoint,
target registration and audit trail must authorize the external sender.
"""
from __future__ import annotations
import re
from urllib.parse import urlsplit


class FeedbackError(ValueError):
    pass


_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}\Z")
_STAGE = {"admission", "security", "sync", "migration", "rollout", "smoke", "rollback", "operational"}
_STATUS = {"pending", "running", "succeeded", "failed", "blocked", "degraded"}
_FAILURE = {"none", "policy", "image", "configuration", "secrets", "migration", "health", "network", "storage", "capacity", "unknown"}
_ACTION = {"none", "review", "approve", "restore", "update-secrets", "retry-manually", "rollback"}


def normalize_feedback(release, event):
    """Return only approved fields after matching immutable originating artifacts.

    The caller authenticates the Raft [R]AP sender and authorizes the
    environment; this function accepts no arbitrary prose or raw log output.
    """
    required = {"release_id", "deployment_id", "environment", "platform", "images",
                "chart_digest", "stage", "status", "failure_class", "diagnostic_ref",
                "human_action"}
    if not isinstance(event, dict) or set(event) != required:
        raise FeedbackError("Feedback must have exactly the approved metadata fields.")
    if event["release_id"] != release.get("release_id"):
        raise FeedbackError("Feedback is not for the originating release.")
    if event["images"] != release.get("images") or event["chart_digest"] != release.get("chart", {}).get("oci_digest"):
        raise FeedbackError("Feedback artifact digests do not match the released OCI artifacts.")
    if not _SHA.fullmatch(event["chart_digest"] or "") or any(
        not isinstance(v, str) or not _SHA.fullmatch(v.rsplit("@", 1)[-1]) for v in event["images"].values()
    ):
        raise FeedbackError("Feedback requires immutable OCI digests.")
    if not all(isinstance(event[key], str) and _ID.fullmatch(event[key])
               for key in ("deployment_id", "environment", "platform")):
        raise FeedbackError("Invalid deployment correlation identifiers.")
    if (event["stage"] not in _STAGE or event["status"] not in _STATUS
            or event["failure_class"] not in _FAILURE or event["human_action"] not in _ACTION):
        raise FeedbackError("Unsupported diagnostic classification.")
    # Only opaque receipt IDs or trusted HTTPS log-ticket paths. No query
    # strings, credentials, fragments, IP addresses or raw customer output.
    diagnostic = event["diagnostic_ref"]
    if not isinstance(diagnostic, str) or len(diagnostic) > 256:
        raise FeedbackError("Unsafe diagnostics reference.")
    if diagnostic and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,255}", diagnostic):
        raise FeedbackError("Unsafe diagnostics reference.")
    if diagnostic.startswith("http"):
        uri = urlsplit(diagnostic)
        if uri.scheme != "https" or not uri.hostname or uri.username or uri.password or uri.port:
            raise FeedbackError("Unsafe diagnostics reference.")
    elif diagnostic and not _ID.fullmatch(diagnostic):
        raise FeedbackError("Use an opaque diagnostic receipt identifier.")
    return {key: event[key] for key in sorted(required)}
