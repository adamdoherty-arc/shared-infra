#!/usr/bin/env bash
# Run infractl's test suite in a disposable Linux container that mirrors the
# image layout (infractl imports fcntl, so it cannot run on the Windows host).
# Never touches the running shared-infra-control container.
#   bash scripts/run_infractl_tests.sh            # unit tests only
#   bash scripts/run_infractl_tests.sh -m live    # extra pytest args pass through
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
command -v cygpath >/dev/null 2>&1 && ROOT="$(cygpath -m "$ROOT")"
ARGS=("$@")
[ ${#ARGS[@]} -eq 0 ] && ARGS=(-m "not live")
MSYS_NO_PATHCONV=1 docker run --rm -v "${ROOT}:/src:ro" python:3.12-slim sh -c '
  set -e
  mkdir -p /app/vendor/scripts
  cp -r /src/infractl /app/infractl
  cp /src/infractl/pyproject.toml /app/
  cp /src/vllm_wedge_monitor.py /app/
  cp /src/scripts/export_config_snapshot.py /app/vendor/scripts/
  mkdir -p /app/bifrost && cp /src/bifrost/sync_vk_allowlists.py /src/bifrost/operator-disabled.json /app/bifrost/
  cd /app && pip install -q -e ".[test]" >/dev/null 2>&1
  INFRACTL_VENDORED_SCRIPTS_DIR=/app/vendor/scripts python -m pytest -q -p no:cacheprovider "$@" infractl/tests
' _ "${ARGS[@]}"
