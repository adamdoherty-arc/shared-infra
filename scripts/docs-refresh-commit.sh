#!/usr/bin/env bash
# Commit topic files touched only below the docs_refresh auto marker (hostcron ada-docs-refresh-commit).
set -uo pipefail
mapfile -t paths < <(python /c/code/shared-infra/scripts/ada_docs_refresh_paths.py | tr -d '\r')
exec bash /c/code/shared-infra/scripts/ada-autocommit.sh \
    "Enhancement-1001108: nightly docs_refresh topic sync" \
    "Auto-generated sections (below the auto marker) of .claude/memory/topics refreshed by scripts/docs_refresh.py" "${paths[@]}"
