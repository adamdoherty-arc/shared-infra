"""Nightly retention pruner for Bifrost logs.db.

Bifrost writes every inference request to a SQLite database at
/app/data/logs.db.  Without pruning, this file grows without bound
(it reached 8.3 GB / corrupt on 2026-06-11 before being deleted
manually).  This sidecar runs a DELETE + VACUUM every night at 03:00
UTC to keep the table bounded at a configurable retention window, then
best-effort TRUNCATEs the WAL file (2026-07-13: -wal alone reached
9.76 GB — VACUUM's PASSIVE autocheckpoint reclaims data into the main
file but never shrinks -wal on disk; only a TRUNCATE-mode checkpoint
does, and that needs a momentary exclusive lock a busy Bifrost rarely
leaves open — hence the retry loop below).

Locking strategy:
  logs.db is in WAL mode (confirmed 2026-06-16).  WAL mode allows
  concurrent READERS with no blocking, but a writer (the pruner) still
  needs an exclusive lock for the duration of each DELETE batch.
  Bifrost holds a rolling write transaction while processing requests.
  The pruner uses a generous PRAGMA busy_timeout (default 120 s) so it
  waits for a gap between Bifrost writes rather than failing immediately.
  The nightly schedule at 03:00 UTC is chosen to coincide with the
  quietest traffic window.

  Each DELETE batch targets _BATCH_SIZE rows (default 50 000) so that
  even if Bifrost restarts between batches, rows already deleted are
  committed.  Batched approach also avoids locking for an extended time
  on a single giant DELETE.

Configuration (env vars):
  BIFROST_LOGS_DB          Path to logs.db (default: /data/logs.db)
  LOGS_RETENTION_DAYS      Rows older than this are deleted (default: 7)
  PRUNE_HOUR_UTC           Hour (0-23 UTC) to run the nightly prune (default: 3)
  PRUNE_BATCH_SIZE         Rows per DELETE batch (default: 50000)
  PRUNE_BUSY_TIMEOUT_MS    SQLite busy timeout in ms (default: 120000)
  RUN_ONCE                 If "1", run immediately and exit (for manual/CI use)
  AUTOHEAL_DISCORD_WEBHOOK Optional Discord webhook (shared with auth_autoheal.py's
                           sidecar) -- WAL-size alerts degrade to log-only if unset
  PRUNE_WAL_ALERT_THRESHOLD_MB  Alert if logs.db-wal exceeds this after a checkpoint
                                pass (default: 512)
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOGS_DB = Path(os.getenv("BIFROST_LOGS_DB", "/data/logs.db"))
RETENTION_DAYS = int(os.getenv("LOGS_RETENTION_DAYS", "7"))
PRUNE_HOUR_UTC = int(os.getenv("PRUNE_HOUR_UTC", "3"))
BATCH_SIZE = int(os.getenv("PRUNE_BATCH_SIZE", "50000"))
BUSY_TIMEOUT_MS = int(os.getenv("PRUNE_BUSY_TIMEOUT_MS", "120000"))
RUN_ONCE = os.getenv("RUN_ONCE", "0") == "1"

# 2026-09-05 (Fix-1100000305-followup): the WAL reached 7.46 GB and hung
# every shared-bifrost restart for minutes -- TRUNCATE had been losing its
# race against live traffic on every scheduled run with nothing surfacing
# that fact anywhere but this container's own stdout log, which nobody was
# watching. Same stdlib-only Discord webhook pattern as the sibling
# auth_autoheal.py sidecar (shared env var, no extra dependency) -- an
# empty/unset webhook degrades to log-only, same as that sidecar today.
DISCORD_WEBHOOK = os.environ.get("AUTOHEAL_DISCORD_WEBHOOK", "").strip()
WAL_ALERT_THRESHOLD_MB = float(os.getenv("PRUNE_WAL_ALERT_THRESHOLD_MB", "512"))


def _alert(text: str) -> None:
    print(f"[pruner] ALERT: {text}", flush=True)
    if not DISCORD_WEBHOOK:
        return
    try:
        data = json.dumps({"content": f":warning: bifrost-logs-pruner: {text}"}).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=data, headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15).read()
        print("[pruner] discord alert posted", flush=True)
    except Exception as exc:  # noqa: BLE001 -- alerting must never crash the pruner
        print(f"[pruner] warn: discord webhook failed ({exc!r})", flush=True)


def _file_mb(path: Path) -> float:
    try:
        return path.stat().st_size / 1_048_576
    except OSError:
        return 0.0


def _connect_rw() -> sqlite3.Connection:
    """Open logs.db read-write with WAL mode + generous busy timeout."""
    conn = sqlite3.connect(str(LOGS_DB), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _count_stale(conn: sqlite3.Connection) -> int:
    """Count rows older than RETENTION_DAYS.

    Bifrost's logs.timestamp column is SQLite type ``datetime`` (ISO text,
    e.g. ``'2026-07-13 04:22:54'``).  Comparing it against
    ``strftime('%s', ...)`` (epoch-seconds text like ``'1783229054'``) is a
    lexicographic text comparison — ISO strings starting with ``'2'`` are
    always textually greater than epoch strings starting with ``'1'``, so the
    predicate never matched and nothing was ever pruned.  Use
    ``datetime('now', ?)`` so both sides are ISO strings.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM logs WHERE timestamp < datetime('now', ?)",
        (f"-{RETENTION_DAYS} days",),
    )
    row = cur.fetchone()
    return row[0] if row else 0


def _delete_batch(conn: sqlite3.Connection) -> int:
    """Delete one batch of stale rows.  Returns number deleted."""
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM logs WHERE ROWID IN "
        "(SELECT ROWID FROM logs "
        " WHERE timestamp < datetime('now', ?) "
        " LIMIT ?)",
        (f"-{RETENTION_DAYS} days", BATCH_SIZE),
    )
    deleted = cur.rowcount
    conn.commit()
    return deleted


def _prune_once() -> None:
    """Run batched DELETE + VACUUM against logs.db and log results."""
    if not LOGS_DB.exists():
        print(f"[pruner] logs.db not found at {LOGS_DB} — skipping", flush=True)
        return

    size_before_mb = _file_mb(LOGS_DB)
    print(
        f"[pruner] starting prune: retention={RETENTION_DAYS}d, "
        f"batch={BATCH_SIZE}, busy_timeout={BUSY_TIMEOUT_MS}ms, "
        f"db={LOGS_DB}, size_before={size_before_mb:.1f} MB",
        flush=True,
    )

    try:
        conn = _connect_rw()
        try:
            stale_count = _count_stale(conn)
            print(f"[pruner] stale rows to delete: {stale_count}", flush=True)

            total_deleted = 0
            batch_num = 0
            while True:
                batch_num += 1
                try:
                    deleted = _delete_batch(conn)
                except sqlite3.OperationalError as exc:
                    # Bifrost held the lock beyond busy_timeout — skip remaining batches.
                    # Already-committed batches are permanent.
                    print(
                        f"[pruner] write lock timeout on batch {batch_num} "
                        f"(deleted so far: {total_deleted}): {exc!r}",
                        flush=True,
                    )
                    break
                total_deleted += deleted
                if deleted < BATCH_SIZE:
                    break  # Last batch — done.
                print(
                    f"[pruner] batch {batch_num}: deleted {deleted} rows "
                    f"(cumulative: {total_deleted})",
                    flush=True,
                )
                # Brief pause between batches to yield the write lock to Bifrost.
                time.sleep(0.5)

            if total_deleted > 0:
                # VACUUM reclaims freed pages and shrinks the file.
                # Can also be blocked; if so, we log the warning and skip.
                print(f"[pruner] running VACUUM ...", flush=True)
                try:
                    conn.execute("VACUUM")
                    conn.commit()
                    print("[pruner] VACUUM complete", flush=True)
                except sqlite3.OperationalError as exc:
                    print(f"[pruner] VACUUM skipped (lock contention): {exc!r}", flush=True)
            else:
                print("[pruner] no rows deleted — VACUUM skipped", flush=True)

            # LGN-14184: prior version only tried TRUNCATE (needs an exclusive
            # lock Bifrost never releases -- verified 15+ consecutive failures)
            # and did NOT try PASSIVE/RESTART fallbacks or set
            # journal_size_limit. Result: -wal grew to 6.5-9.7 GB and stayed
            # there indefinitely. New ladder (all non-blocking, all safe under
            # continuous Bifrost writes):
            #   1. PASSIVE checkpoint (never blocks -- moves committed pages
            #      into main file even while Bifrost writes continue)
            #   2. RESTART checkpoint (like PASSIVE + tells future writers to
            #      start a fresh WAL block, so the WAL file eventually shrinks
            #      once existing readers drain their snapshots -- no exclusive
            #      lock required)
            #   3. TRUNCATE (best-effort as before -- shrinks file to zero
            #      immediately when it succeeds)
            #   4. Set journal_size_limit so next writer commit trims the WAL
            #      to that cap on disk. Persists in the DB file header.
            wal_reclaimed = False
            wal_cur = conn.cursor()
            wal_mb_before = _file_mb(LOGS_DB.with_name(LOGS_DB.name + "-wal"))

            # Cap WAL file at 512 MB via journal_size_limit (persistent DB header setting).
            try:
                wal_cur.execute("PRAGMA journal_size_limit = 536870912")
                jsl_val = wal_cur.fetchone()
                print(f"[pruner] PRAGMA journal_size_limit -> {jsl_val[0] if jsl_val else '?'} bytes", flush=True)
            except sqlite3.OperationalError as exc:
                print(f"[pruner] journal_size_limit set failed: {exc!r}", flush=True)

            # Step 1: PASSIVE (never blocks, moves committed pages into main)
            try:
                wal_cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
                busy_p, frames_p, checkpointed_p = wal_cur.fetchone()
                print(
                    f"[pruner] wal_checkpoint(PASSIVE): busy={busy_p} "
                    f"frames={frames_p} checkpointed={checkpointed_p}",
                    flush=True,
                )
            except sqlite3.OperationalError as exc:
                print(f"[pruner] wal_checkpoint(PASSIVE) error: {exc!r}", flush=True)

            # Step 2: RESTART (no exclusive lock, forces future writers to
            # rotate the WAL block so existing WAL file can shrink)
            try:
                wal_cur.execute("PRAGMA wal_checkpoint(RESTART)")
                busy_r, frames_r, checkpointed_r = wal_cur.fetchone()
                print(
                    f"[pruner] wal_checkpoint(RESTART): busy={busy_r} "
                    f"frames={frames_r} checkpointed={checkpointed_r}",
                    flush=True,
                )
            except sqlite3.OperationalError as exc:
                print(f"[pruner] wal_checkpoint(RESTART) error: {exc!r}", flush=True)

            # Step 3: TRUNCATE (best-effort, as before) -- up to 5 attempts
            for attempt in range(1, 6):
                try:
                    wal_cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    busy, log_frames, checkpointed = wal_cur.fetchone()
                    if busy == 0:
                        print(
                            f"[pruner] wal_checkpoint(TRUNCATE) succeeded on "
                            f"attempt {attempt} (frames={log_frames}, "
                            f"checkpointed={checkpointed})",
                            flush=True,
                        )
                        wal_reclaimed = True
                        break
                    print(
                        f"[pruner] wal_checkpoint(TRUNCATE) attempt {attempt} "
                        f"busy (frames={log_frames}, checkpointed={checkpointed}) "
                        "— Bifrost mid-write, retrying",
                        flush=True,
                    )
                except sqlite3.OperationalError as exc:
                    print(
                        f"[pruner] wal_checkpoint(TRUNCATE) attempt {attempt} "
                        f"error: {exc!r}",
                        flush=True,
                    )
                time.sleep(3)

            wal_mb_after = _file_mb(LOGS_DB.with_name(LOGS_DB.name + "-wal"))
            if wal_reclaimed:
                print(
                    f"[pruner] WAL reclaim summary: {wal_mb_before:.1f} MB -> "
                    f"{wal_mb_after:.1f} MB (TRUNCATE succeeded)",
                    flush=True,
                )
            else:
                # journal_size_limit + RESTART/PASSIVE still bound growth even
                # when TRUNCATE loses the race -- pages ARE in the main file.
                print(
                    f"[pruner] WAL reclaim summary: {wal_mb_before:.1f} MB -> "
                    f"{wal_mb_after:.1f} MB (TRUNCATE lost race after 5 "
                    "attempts; PASSIVE+RESTART checkpointed pages into main "
                    "regardless; journal_size_limit=512 MB will trim on next "
                    "writer commit boundary).",
                    flush=True,
                )
            if wal_mb_after > WAL_ALERT_THRESHOLD_MB:
                _alert(
                    f"logs.db-wal is {wal_mb_after:.0f} MB after tonight's prune "
                    f"(threshold {WAL_ALERT_THRESHOLD_MB:.0f} MB) -- TRUNCATE "
                    f"{'succeeded' if wal_reclaimed else 'lost its race against live traffic'}. "
                    "A WAL this size can hang shared-bifrost's next restart for minutes "
                    "(see the 2026-09-05 incident); consider a brief Bifrost pause during "
                    "the next prune if this recurs."
                )
        finally:
            conn.close()
    except Exception as exc:
        print(f"[pruner] ERROR connecting to db: {exc!r}", flush=True)
        return

    size_after_mb = _file_mb(LOGS_DB)
    reclaimed_mb = size_before_mb - size_after_mb
    print(
        f"[pruner] done: total_deleted={total_deleted} "
        f"size_after={size_after_mb:.1f} MB "
        f"reclaimed={reclaimed_mb:.1f} MB",
        flush=True,
    )


def _seconds_until_next_prune() -> float:
    """Return seconds until the next PRUNE_HOUR_UTC:00:00 UTC."""
    now_utc = datetime.now(timezone.utc)
    target_today = now_utc.replace(
        hour=PRUNE_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    delta = (target_today - now_utc).total_seconds()
    if delta <= 0:
        # Already past today's window — next run is tomorrow.
        delta += 86_400
    return delta


# LGN-14184: interval (seconds) between mid-day PASSIVE checkpoint passes.
# 15 min was picked so a saturated Bifrost never grows the WAL more than
# ~15 min worth of pages beyond the journal_size_limit ceiling.
_MID_DAY_CHECKPOINT_INTERVAL_S = int(os.getenv("WAL_MIDDAY_CHECKPOINT_INTERVAL_S", "900"))


def _mid_day_checkpoint() -> None:
    """LGN-14184: PASSIVE + RESTART checkpoint pass, no exclusive lock needed.

    Runs on a mid-day loop between nightly prunes so the WAL file never grows
    unbounded even if the nightly TRUNCATE consistently loses the race. Never
    blocks Bifrost -- PASSIVE walks committed pages into the main file while
    writes continue; RESTART tells future writers to rotate the WAL block.
    """
    try:
        conn = _connect_rw()
    except Exception as exc:
        print(f"[pruner] midday connect failed: {exc!r}", flush=True)
        return
    try:
        wal_mb_before = _file_mb(LOGS_DB.with_name(LOGS_DB.name + "-wal"))
        cur = conn.cursor()
        try:
            cur.execute("PRAGMA journal_size_limit = 536870912")
            cur.fetchone()
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
            busy_p, frames_p, checkpointed_p = cur.fetchone()
            try:
                cur.execute("PRAGMA wal_checkpoint(RESTART)")
                busy_r, frames_r, checkpointed_r = cur.fetchone()
            except sqlite3.OperationalError:
                busy_r = frames_r = checkpointed_r = -1
            wal_mb_after = _file_mb(LOGS_DB.with_name(LOGS_DB.name + "-wal"))
            print(
                f"[pruner] midday checkpoint: WAL {wal_mb_before:.1f} MB -> "
                f"{wal_mb_after:.1f} MB (PASSIVE busy={busy_p} "
                f"frames={frames_p} chkpt={checkpointed_p}; "
                f"RESTART busy={busy_r} frames={frames_r} chkpt={checkpointed_r})",
                flush=True,
            )
            if wal_mb_after > WAL_ALERT_THRESHOLD_MB:
                _alert(
                    f"logs.db-wal is {wal_mb_after:.0f} MB at a midday checkpoint "
                    f"(threshold {WAL_ALERT_THRESHOLD_MB:.0f} MB) -- PASSIVE/RESTART "
                    "alone are not shrinking it fast enough between nightly prunes."
                )
        except sqlite3.OperationalError as exc:
            print(f"[pruner] midday checkpoint error: {exc!r}", flush=True)
    finally:
        conn.close()


def main() -> None:
    if RUN_ONCE:
        print("[pruner] RUN_ONCE=1 — running prune immediately", flush=True)
        _prune_once()
        return

    print(
        f"[pruner] started — nightly prune at {PRUNE_HOUR_UTC:02d}:00 UTC, "
        f"retention={RETENTION_DAYS}d, batch={BATCH_SIZE}, db={LOGS_DB}; "
        f"midday PASSIVE checkpoint every {_MID_DAY_CHECKPOINT_INTERVAL_S}s "
        "(LGN-14184)",
        flush=True,
    )
    while True:
        wait_s = _seconds_until_next_prune()
        print(
            f"[pruner] sleeping up to {wait_s / 3600:.2f}h until next prune "
            f"(target: {PRUNE_HOUR_UTC:02d}:00 UTC), "
            f"waking every {_MID_DAY_CHECKPOINT_INTERVAL_S}s for a PASSIVE "
            "checkpoint",
            flush=True,
        )
        # Slice sleep into midday-checkpoint intervals.
        end_at = time.monotonic() + wait_s
        while True:
            slice_s = min(_MID_DAY_CHECKPOINT_INTERVAL_S, max(0, end_at - time.monotonic()))
            if slice_s <= 0:
                break
            time.sleep(slice_s)
            if time.monotonic() >= end_at:
                break
            _mid_day_checkpoint()
        _prune_once()


if __name__ == "__main__":
    main()
