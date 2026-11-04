"""AWS runner adapter for the existing application-release pipeline.

CodeConnections downloads both private repositories. Only an operator-authorized
temporary Secrets Manager credential is used for GitHub Actions admission reads.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def run():
    root = Path(__file__).resolve().parents[1]
    mode = os.environ.get("NCE_RELEASE_MODE", "verify")
    if mode not in ("verify", "publish"):
        raise SystemExit("Invalid release mode.")
    source = Path(os.environ["CODEBUILD_SRC_DIR_ProposalStudio"])
    for path, version in ((root, os.environ.get("CODEBUILD_RESOLVED_SOURCE_VERSION", "")),
                          (source, os.environ.get("NCE_APPLICATION_COMMIT", ""))):
        if not re.fullmatch(r"[a-f0-9]{40}", version):
            raise SystemExit("An exact source commit is required.")
        actual = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
        if actual != version:
            raise SystemExit("The downloaded source differs from the approved commit.")
    environment = os.environ.copy()
    environment["PATH"] = "/tmp/nce-oci-tools:" + environment["PATH"]
    environment["CORE_CI_ENVIRONMENT"] = "production"
    environment["CORE_CI_ALLOW_PUBLISH"] = "1" if mode == "publish" else "0"
    environment["CORE_CI_ALLOW_DEPLOY"] = "0"
    # Never expose the credential as an environment value or command argument.
    with tempfile.TemporaryDirectory(prefix="nce-oci-admission-") as private:
        token_path = Path(private) / "ci-read"
        token_path.touch(mode=0o600)
        with token_path.open("w") as output:
            result = subprocess.run([
                "aws", "secretsmanager", "get-secret-value",
                "--secret-id", os.environ["NCE_CI_READ_SECRET_ARN"],
                "--query", "SecretString", "--output", "text", "--region", "us-east-1",
            ], stdout=output, stderr=subprocess.PIPE)
        if result.returncode:
            raise SystemExit("The operator-authorized CI read credential is unavailable.")
        environment["CORE_CI_GITHUB_TOKEN_FILE"] = str(token_path)
        result = subprocess.run([
            "python3", "ci/pipeline.py", "application-release", "--config",
            "ci/applications/proposal-studio.production.json", "--source", str(source),
            "--mode", mode,
        ], cwd=root, env=environment)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    run()
