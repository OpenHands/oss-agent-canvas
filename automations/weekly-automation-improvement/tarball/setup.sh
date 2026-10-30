#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

SDK_VERSION=$(curl -fsS "${AUTOMATION_API_URL}/sdk-version" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
uv venv .venv --python '>=3.12' --quiet --allow-existing
uv pip install --python .venv/bin/python --quiet \
  "openhands-sdk==${SDK_VERSION}" \
  "openhands-tools==${SDK_VERSION}" \
  "openhands-workspace==${SDK_VERSION}"
