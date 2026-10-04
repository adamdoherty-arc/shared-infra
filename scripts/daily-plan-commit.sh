#!/usr/bin/env bash
# Land the daily plan files ADA's scheduler writes and never commits (hostcron ada-daily-plan-commit).
# daily_plan_render (06:20 ET, ada-scheduler) writes docs/plans/daily/YYYY-MM-DD.md; this also regenerates
# STRUCTURE.md's generated section from the host. Both go through ada-autocommit.sh (safe_commit.sh).
# A daily file is committed only while its first line is the generated title (a session's hand-written
# day file is that session's to land). STRUCTURE.md is committed only on a clean render (exit 0) and only
# when its table changed or the committed stamp is 3+ days old, so the 7-day pre-push freshness gate
# never blocks a push again.
set -uo pipefail
cd /c/code/ADA || exit 1
paths=()
while IFS= read -r line; do
    p="${line:3}"
    case "$p" in
        docs/plans/daily/20[0-9][0-9]-[0-1][0-9]-[0-3][0-9].md)
            head -1 "$p" 2>/dev/null | grep -qE '^# 20[0-9]{2}-[0-9]{2}-[0-9]{2}: live probes' && paths+=("$p") ;;
    esac
done < <(git status --porcelain --untracked-files=all -- docs/plans/daily)
struct=docs/plans/daily/STRUCTURE.md
python scripts/daily_plan.py --structure
src=$?
if [ "$src" -eq 0 ] && [ -n "$(git status --porcelain -- "$struct")" ]; then
    table_changed=$(git diff -U0 -- "$struct" | grep -cE '^[+-][|]')
    stamp=$(git show "HEAD:$struct" 2>/dev/null | grep -oE '_generated: [0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1 | cut -c13-)
    age_days=99
    if [ -n "$stamp" ]; then
        age_days=$(( ( $(date +%s) - $(date -d "$stamp" +%s) ) / 86400 ))
    fi
    echo "daily-plan-commit: structure table lines changed=$table_changed, committed stamp $stamp is $age_days days old"
    if [ "$table_changed" -gt 0 ] || [ "$age_days" -ge 3 ]; then
        paths+=("$struct")
    else
        other_changed=$(git diff -U0 -- "$struct" | grep -E '^[+-]' | grep -vE '^(\+\+\+|---)' | grep -vc '_generated:')
        if [ "$other_changed" -eq 0 ]; then
            git checkout -- "$struct"
            echo "daily-plan-commit: only the stamp moved; STRUCTURE.md restored to HEAD"
        fi
    fi
elif [ "$src" -ne 0 ]; then
    echo "daily-plan-commit: structure render exit $src (2 = database unreachable); STRUCTURE.md not committed"
fi
exec bash /c/code/shared-infra/scripts/ada-autocommit.sh \
    "Daily plan: land the generated daily files" \
    "Committed by hostcron ada-daily-plan-commit (daily_plan_render output and a fresh STRUCTURE.md)" "${paths[@]}"
