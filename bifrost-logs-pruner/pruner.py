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
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

LOGS_DB = Path(os.getenv("BIFROST_LOGS_DB", "/data/logs.db"))
RETENTION_DAYS = int(os.getenv("LOGS_RETENTION_DAYS", "7"))
PRUNE_HOUR_UTC = int(os.getenv("PRUNE_HOUR_UTC", "3"))
BATCH_SIZE = int(os.getenv("PRUNE_BATCH_SIZE", "50000"))
BUSY_TIMEOUT_MS = int(os.getenv("PRUNE_BUSY_TIMEOUT_MS", "120000"))
RUN_ONCE = os.getenv("RUN_ONCE", "0") == "1"


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

            # WAL-mode gotcha (found 2026-07-13, logs.db hygiene workstream):
            # VACUUM writes its rewritten pages through the WAL like any other
            # transaction. A PASSIVE checkpoint (SQLite's default autocheckpoint,
            # fires every ~1000 WAL pages) reclaims that data into the main file
            # but does NOT shrink logs.db-wal on disk — only a TRUNCATE-mode
            # checkpoint does, and TRUNCATE needs a momentary exclusive lock that
            # a continuously-writing Bifrost rarely leaves open. Left unaddressed,
            # -wal grew to 9.76 GB (bigger than the 11.95 GB main file) with zero
            # visibility, because bifrost_logs_db_bytes only measured logs.db
            # itself. Best-effort: try a few times with short backoffs — succeeds
            # whenever Bifrost happens to be between writes, harmless no-op
            # (busy=1) otherwise. Never blocks Bifrost — busy_timeout still caps
            # the wait, and TRUNCATE failing just leaves the WAL as-is.
            wal_reclaimed = False
            wal_cur = conn.cursor()
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
            if not wal_reclaimed:
                wal_mb = _file_mb(LOGS_DB.with_name(LOGS_DB.name + "-wal"))
                print(
                    f"[pruner] wal_checkpoint(TRUNCATE) did not complete after "
                    f"5 attempts — logs.db-wal stays at {wal_mb:.1f} MB on disk "
                    "(data itself is safely checkpointed into logs.db; this "
                    "only affects on-disk WAL file size, not correctness). "
                    "Will retry tomorrow.",
                    flush=True,
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


def main() -> None:
    if RUN_ONCE:
        print("[pruner] RUN_ONCE=1 — running prune immediately", flush=True)
        _prune_once()
        return

    print(
        f"[pruner] started — nightly prune at {PRUNE_HOUR_UTC:02d}:00 UTC, "
        f"retention={RETENTION_DAYS}d, batch={BATCH_SIZE}, db={LOGS_DB}",
        flush=True,
    )
    while True:
        wait_s = _seconds_until_next_prune()
        print(
            f"[pruner] sleeping {wait_s / 3600:.2f}h until next prune "
            f"(target: {PRUNE_HOUR_UTC:02d}:00 UTC)",
            flush=True,
        )
        time.sleep(wait_s)
        _prune_once()


if __name__ == "__main__":
    main()
