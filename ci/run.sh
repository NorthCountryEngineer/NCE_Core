#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Isolate the pinned bootstrap tool; never modify the runner's global Python.
python3 -m venv .ci-tools
.ci-tools/bin/python -m pip install --disable-pip-version-check 'uv==0.12.19'
if [[ "${CORE_CI_CHAT_DEPLOYMENT:-0}" == "1" ]]; then
  .ci-tools/bin/uv sync --frozen --group browser
  .venv/bin/python -m playwright install "${CORE_CI_CHAT_BROWSER:-chromium}"
else
  .ci-tools/bin/uv sync --frozen
fi
.venv/bin/python ci/pipeline.py run \
  --config "${CORE_CI_CONFIG:-ci/environments/example.json}" \
  --mode "${CORE_CI_MODE:-verify}"
