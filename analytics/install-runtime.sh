#!/bin/sh
# Same init command for Kubernetes and CI. No credentials are supplied here.
set -eu
python -m venv /opt/runtime/venv
/opt/runtime/venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir --no-compile --only-binary=:all: --require-hashes -r "${REQUIREMENTS_FILE:-/opt/analytics/requirements.lock}"
