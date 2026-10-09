"""Scan tracked files without printing credential values or making verifier calls."""
from contextlib import chdir
from pathlib import Path
import subprocess
import sys

from detect_secrets import SecretsCollection
from detect_secrets.core import baseline

ROOT = Path(__file__).resolve().parent.parent


def scan():
    files = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True,
                           stdout=subprocess.PIPE).stdout.decode().split('\0')
    # Loading settings from the reviewed baseline preserves --no-verify filters.
    approved = baseline.load(baseline.load_from_file(str(ROOT / '.secrets.baseline')),
                             filename='.secrets.baseline')
    findings = SecretsCollection()
    with chdir(ROOT):
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
    sys.exit(scan())
