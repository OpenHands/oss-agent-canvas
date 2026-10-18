#!/bin/sh
uv venv .venv --python 'cpython>=3.12,<3.14' --quiet || exit 1
uv pip install --python .venv/bin/python --index-url https://pypi.org/simple 'openhands-sdk==1.54.0' 'openhands-tools==1.54.0' 'openhands-workspace==1.54.0' || exit 1
OPENHANDS_SUPPRESS_BANNER=1 .venv/bin/python main.py --preflight
