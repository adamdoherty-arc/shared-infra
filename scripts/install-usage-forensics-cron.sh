#!/usr/bin/env bash
# Install the weekly Claude usage forensics on macOS/Linux (Sunday 08:00 local) as a cron line.
# Idempotent: re-running replaces the existing line. Run from any directory.
set -eu
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$REPO/scripts/run-usage-forensics.sh"
chmod +x "$RUNNER" "$REPO/scripts/install-usage-forensics-cron.sh"
LINE="0 8 * * 0 /usr/bin/env bash \"$RUNNER\" # claude-usage-forensics-weekly"
( crontab -l 2>/dev/null | grep -v 'claude-usage-forensics-weekly' ; echo "$LINE" ) | crontab -
echo "installed: $(crontab -l | grep claude-usage-forensics-weekly)"
echo "first run now (takes a minute, writes docs/claude-usage/hosts/$(hostname -s)/):"
bash "$RUNNER" && echo "ok -- now: cd \"$REPO\" && git push origin master"
