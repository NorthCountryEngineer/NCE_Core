"""Trusted frontend entrypoint for the isolated, artifact-only CodeBuild role.

This file runs repository npm scripts ONLY in that separate project. It must
never be used by the privileged deployment runner. Its output contains compiled
public frontend files, never credentials or infrastructure command results.
"""
import base64
import json
import os
import re
import subprocess
import boto3
from botocore.config import Config
from zipfile import ZipFile, ZIP_DEFLATED
from pathlib import Path


def safe_directory(root, value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", value):
        raise ValueError("Invalid frontend directory.")
    target = (root / value).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_dir():
        raise ValueError("Frontend directory must exist inside the repository.")
    return target


def main():
    config = json.loads(base64.b64decode(os.environ["NCE_BUILD_CONFIG"], validate=True))
    root = Path(os.environ["CODEBUILD_SRC_DIR_APPLICATION"]).resolve()
    source = safe_directory(root, config["frontend"]["directory"])
    environment = dict(os.environ)
    # Defense in depth; role isolation remains the actual security boundary.
    for name, value in config["public_config"].items():
        if not re.fullmatch(r"VITE_[A-Z0-9_]{1,100}", name) or re.search(r"SECRET|PASSWORD|TOKEN|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY", name):
            raise ValueError("Only public frontend configuration is allowed.")
        environment[name] = value
    subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=source, env=environment, check=True)
    subprocess.run(["npm", "run", "build"], cwd=source, env=environment, check=True)
    output = safe_directory(source, config["frontend"]["output"])
    if not (output / "index.html").is_file():
        raise ValueError("Frontend build must contain index.html.")
    # The fixed project buildspec collects only this directory. No app chosen
    # buildspec, infrastructure template, or command is ever accepted.
    destination = Path(os.environ["CODEBUILD_SRC_DIR"]) / "nce-frontend"
    import shutil
    if destination.exists():
        shutil.rmtree(destination)
    if any(path.is_symlink() for path in output.rglob("*")):
        raise ValueError("Frontend output must not contain symbolic links.")
    shutil.copytree(output, destination, symlinks=False)
    token = os.environ["NCE_RUN_TOKEN"]
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError("Invalid source build token.")
    archive = Path("/tmp/nce-frontend.zip")
    size = 0
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as packed:
        for path in sorted(destination.rglob("*")):
            if path.is_symlink():
                raise ValueError("Frontend output must not contain symbolic links.")
            if path.is_file():
                size += path.stat().st_size
                if size > 200 * 1024 * 1024:
                    raise ValueError("Frontend output exceeds the deployment size limit.")
                packed.write(path, str(path.relative_to(destination)))
    session = boto3.Session(region_name=os.environ["AWS_DEFAULT_REGION"])
    client = session.client("s3", config=Config(connect_timeout=5, read_timeout=30, retries={"mode":"standard","total_max_attempts":3}))
    client.upload_file(str(archive), os.environ["NCE_ARTIFACT_BUCKET"], f"builds/{token}/frontend.zip",
        ExtraArgs={"ServerSideEncryption":"AES256", "ContentType":"application/zip"})


if __name__ == "__main__":
    main()
