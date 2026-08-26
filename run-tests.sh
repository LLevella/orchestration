#!/usr/bin/env bash
# Orchestration unit + integration tests — stdlib Python only.
# No pip deps, no network, no AI credentials (mock workflows run scripted fixtures).
# Used by Jenkins (CI gate) and the .githooks/pre-commit hook. Runnable from anywhere.
set -euo pipefail
cd "$(dirname "$0")"            # the orchestration/ directory
echo "[orch-tests] $(python3 --version 2>&1)"
exec python3 -m unittest discover -s tests -t . -v
