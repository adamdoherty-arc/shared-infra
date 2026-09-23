"""Probe modules. Each exposes `probe(settings) -> dict` returning
`{name, ok, detail, latency_ms, ts}`. `registry.run_all()` runs every probe,
catching per-probe exceptions so one broken probe never blocks the rest, and
writes each result to the ledger's `probe_runs` table."""
