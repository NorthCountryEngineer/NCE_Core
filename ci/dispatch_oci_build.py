"""Dispatch the existing OCI release pipeline to its fixed NCE AWS builder."""
import argparse
import json
import os
import re
import subprocess
import time

PROJECT = "nce-proposal-oci-release"
REGION = "us-east-1"


def aws(*arguments):
    result = subprocess.run(["aws", *arguments, "--region", REGION, "--output", "json"],
                            capture_output=True, text=True)
    if result.returncode:
        raise SystemExit("The NCE build control operation failed.")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-commit", required=True)
    parser.add_argument("--runner-commit", required=True)
    parser.add_argument("--mode", choices=("verify", "publish"), default="verify")
    args = parser.parse_args()
    if not all(re.fullmatch(r"[a-f0-9]{40}", value) for value in (args.application_commit, args.runner_commit)):
        raise SystemExit("Exact application and runner commits are required.")
    build = aws("codebuild", "start-build", "--project-name", PROJECT,
        "--source-version", args.runner_commit,
        "--secondary-sources-version-override", json.dumps([{
            "sourceIdentifier": "ProposalStudio", "sourceVersion": args.application_commit}]),
        "--environment-variables-override", json.dumps([
            {"name": "NCE_RELEASE_MODE", "value": args.mode, "type": "PLAINTEXT"},
            {"name": "NCE_APPLICATION_COMMIT", "value": args.application_commit, "type": "PLAINTEXT"},
        ]))["build"]
    build_id = build["id"]
    print("NCE CodeBuild: " + build_id, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write("NCE CodeBuild: `" + build_id + "`\n")
    deadline = time.monotonic() + 85 * 60
    while time.monotonic() < deadline:
        current = aws("codebuild", "batch-get-builds", "--ids", build_id)["builds"]
        if len(current) != 1 or current[0]["id"] != build_id:
            raise SystemExit("NCE returned ambiguous build identity.")
        if current[0].get("buildComplete"):
            status = current[0]["buildStatus"]
            print("NCE CodeBuild status: " + status, flush=True)
            if status != "SUCCEEDED":
                raise SystemExit("The NCE release failed; review its private AWS build logs.")
            return
        time.sleep(10)
    raise SystemExit("The NCE build is still running; inspect AWS before starting another release.")


if __name__ == "__main__":
    main()
