#!/usr/bin/env python3
"""Publish fixed-service container health only; no app data, env or log output."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

HOME = Path('/srv/nce')
SERVICES = ('core-web', 'core-worker', 'proposal-api', 'proposal-worker', 'sam-browser', 'academy-api', 'postgres', 'proxy')


def collect(run=subprocess.run):
    values = {}
    for service in SERVICES:
        result = run(['docker', 'compose', '-p', 'nce', '-f', str(HOME / 'compose.yml'), 'ps', '--all', '--quiet', service],
                     check=True, capture_output=True, text=True, timeout=15)
        ids = result.stdout.split()
        if len(ids) != 1:
            values[service] = {'state': 'unavailable', 'health': 'unknown'}
            continue
        # Go's template selects only public state; inspect environment/logs are
        # never returned, serialized or passed through a caller's diagnostics.
        result = run(['docker', 'inspect', '--format', '{{json .State.Status}} {{if .State.Health}}{{json .State.Health.Status}}{{else}}"unknown"{{end}}', ids[0]],
                     check=True, capture_output=True, text=True, timeout=15)
        state, health = result.stdout.strip().split(' ', 1)
        state, health = json.loads(state), json.loads(health)
        values[service] = {'state': state if state in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'} else 'unknown',
                           'health': health if health in {'starting', 'healthy', 'unhealthy'} else 'unknown'}
    return {'version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(), 'services': values,
            'maintenance': {kind: (HOME / 'maintenance' / kind).exists() for kind in ('core', 'proposal', 'academy')}}


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise RuntimeError('Fixed root-only status publisher required')
    path = HOME / 'status/runtime-health.json'
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as target:
        json.dump(collect(), target, sort_keys=True)
    temporary.chmod(0o644)
    os.replace(temporary, path)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Leave the previous timestamp to become visibly stale. Docker or
        # application diagnostics are never exposed by this publisher.
        raise SystemExit(1)
