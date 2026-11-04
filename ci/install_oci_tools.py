"""Install checksum-pinned release tools in a private runner directory."""
import hashlib
import argparse
import io
import os
from pathlib import Path
import platform
import tarfile
from urllib.request import urlopen


TOOLS = {
    "helm": (
        "https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz",
        "a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496",  # pragma: allowlist secret -- public Helm archive checksum
        "linux-amd64/helm",
    ),
    "kubeconform": (
        "https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz",
        "95f14e87aa28c09d5941f11bd024c1d02fdc0303ccaa23f61cef67bc92619d73",  # pragma: allowlist secret -- public kubeconform archive checksum
        "kubeconform",
    ),
    "cosign": (
        "https://github.com/sigstore/cosign/releases/download/v2.5.3/cosign-linux-amd64",
        "783b5d6c74105401c63946c68d9b2a4e1aab3c8abce043e06b8510b02b623ec9",  # pragma: allowlist secret -- public Cosign binary checksum
        None,
    ),
    "syft": (
        "https://github.com/anchore/syft/releases/download/v1.56.0/syft_1.56.0_linux_amd64.tar.gz",
        "1c2493cfcf52d3367d6cb25780d9190c111c2219c34c820574c43e8bae15c528",  # pragma: allowlist secret -- public Syft archive checksum
        "syft",
    ),
}


def install(destination, *, validation_only=False):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise SystemExit("The production builder requires Linux x86_64.")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, (url, digest, member) in TOOLS.items():
        if validation_only and name not in ("helm", "kubeconform"):
            continue
        with urlopen(url, timeout=90) as response:
            data = response.read(150 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit("Release tool checksum mismatch: " + name)
        if member:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                data = archive.extractfile(member).read()
        target = destination / name
        target.write_bytes(data)
        target.chmod(0o700)
        print("Installed checksum-verified " + name, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-only", action="store_true")
    options = parser.parse_args()
    install(os.environ.get("NCE_OCI_TOOL_DIR", "/tmp/nce-oci-tools"), validation_only=options.validation_only)
