#!/usr/bin/env bash
# macOS/Linux twin of run-usage-forensics.cmd: weekly Claude Code usage page for THIS host.
# Transcripts live under ~/.claude/projects on each machine, so each machine runs its own
# copy (install with scripts/install-usage-forensics-cron.sh). Pure Python, zero tokens.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="$(hostname -s 2>/dev/null || hostname)"
OUT="$REPO/docs/claude-usage/hosts/$HOST"
LOG="$REPO/state/claude-usage/usage-forensics.log"
mkdir -p "$OUT/history" "$(dirname "$LOG")"
cd "$REPO" || exit 1
echo "[$(date -u +%FT%TZ)] === claude usage forensics start ($HOST) ===" >> "$LOG"
python3 "$REPO/scripts/claude_usage_forensics.py" --days 7 --json "$OUT/latest.json" --history-dir "$OUT/history" --markdown "$OUT/README.md" --discord >> "$LOG" 2>&1
RC=$?
echo "[$(date -u +%FT%TZ)] forensics exited $RC" >> "$LOG"
if [ "$RC" -ne 0 ]; then echo "[$(date -u +%FT%TZ)] === done rc=$RC ===" >> "$LOG"; exit "$RC"; fi
python3 "$REPO/scripts/claude_usage_index.py" >> "$LOG" 2>&1
# Path-scoped commit only -- never `git add -A`; other sessions keep dirty files here.
git add -- docs/claude-usage >> "$LOG" 2>&1
if git diff --cached --quiet -- docs/claude-usage; then
  echo "[$(date -u +%FT%TZ)] nothing new to commit" >> "$LOG"
else
  git commit -m "claude-usage: weekly forensics $HOST $(date -u +%F)" -- docs/claude-usage >> "$LOG" 2>&1
  echo "[$(date -u +%FT%TZ)] commit exited $? (files are on disk regardless)" >> "$LOG"
fi
echo "[$(date -u +%FT%TZ)] === claude usage forensics done rc=$RC ===" >> "$LOG"
exit "$RC"
