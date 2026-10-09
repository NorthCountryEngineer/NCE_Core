#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Isolate the pinned bootstrap tool; never modify the runner's global Python.
python3 -m venv .ci-tools
.ci-tools/bin/python -m pip install --disable-pip-version-check 'uv==0.12.19'
.ci-tools/bin/uv sync --frozen
.venv/bin/python ci/pipeline.py run \
  --config "${CORE_CI_CONFIG:-ci/environments/example.json}" \
  --mode "${CORE_CI_MODE:-verify}"
