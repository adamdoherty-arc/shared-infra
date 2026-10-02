#!/usr/bin/env bash
# Shared helper for ADA's generated-file nightly committers (feature map, docs_refresh).
# Usage: ada-autocommit.sh "<subject>" "<body sentence>" <path> [<path> ...]
# Commits ONLY the explicit paths through ADA's safe_commit.sh (full gate chain, isolated index).
# No paths -> "nothing changed", exit 0.
set -uo pipefail
subject="$1"; body="$2"; shift 2
if ! gerr=$(git -C /c/code/ADA rev-parse --git-dir 2>&1 >/dev/null); then
    echo "ada-autocommit: git unusable, refusing to report nothing changed: $gerr" >&2
    exit 2
fi
if [ "$#" -eq 0 ]; then
    echo "ada-autocommit: nothing changed"
    exit 0
fi
cd /c/code/ADA || exit 1
msg="$(mktemp)"
printf '%s\n\n%s (%s files).\n\nLegion #product_feature:audit-hygiene\n\nLegion-Task: 15190\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' "$subject" "$body" "$#" > "$msg"
bash scripts/safe_commit.sh "$msg" "$@"
rc=$?
rm -f "$msg"
exit $rc
