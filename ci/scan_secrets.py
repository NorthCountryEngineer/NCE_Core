"""Scan tracked files without printing credential values or making verifier calls."""
import argparse
from contextlib import chdir, nullcontext
from pathlib import Path
import subprocess
import sys
import tempfile

from detect_secrets import SecretsCollection
from detect_secrets.core import baseline

ROOT = Path(__file__).resolve().parent.parent


def scan(*, staged=False):
    files = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True,
                           stdout=subprocess.PIPE).stdout.decode().split('\0')
    # Loading settings from the reviewed baseline preserves --no-verify filters.
    approved = baseline.load(baseline.load_from_file(str(ROOT / '.secrets.baseline')),
                             filename='.secrets.baseline')
    findings = SecretsCollection()
    # A commit hook must inspect index blobs: the working copy can differ.
    with tempfile.TemporaryDirectory(prefix='core-staged-scan-') if staged else nullcontext(str(ROOT)) as directory:
        source_root = Path(directory)
        if staged:
            for name in files:
                if not name:
                    continue
                target = source_root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                blob = subprocess.run(['git', 'show', ':' + name], cwd=ROOT,
                                      check=True, stdout=subprocess.PIPE).stdout
                target.write_bytes(blob)
                target.chmod(0o600)
        with chdir(source_root):
            for name in files:
                if name:
                    findings.scan_file(name)
    new_findings = findings - approved
    if new_findings:
        for _, finding in new_findings:
            print(f'Secret scanning rejected {finding.filename}:{finding.line_number} ({finding.type}).', file=sys.stderr)
        return 1
    print('Tracked-file secret scan passed.')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true')
    sys.exit(scan(staged=parser.parse_args().staged))
