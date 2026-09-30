# Ops thresholds and who checks them

Written 2026-09-30 (Legion sprint 15209, Dependability W2). Every number here is enforced by
`observability/prometheus/rules/ops_dependability.yml` (Prometheus -> Alertmanager -> direct
Discord receiver `discord-ops`) AND, independently, by the nightly `scripts/ops_selfcheck.py`
(hostcron job `ops-selfcheck`, 11:45 local) which posts to the infra webhook itself.

| Signal | Threshold | Why this number | Checked by |
|---|---|---|---|
| ADA / Legion newest dump age | > 30 h | daily cadence + one missed run of grace | `BackupStale`, `LegionBackupStale`, selfcheck, docker healthchecks (`-mmin -1800`) |
| Off-VHDX copy age | > 36 h | runs 10:30 daily after the 03:30 ET dump | `OffVolumeBackupStale`, selfcheck |
| DR restore drill | last ok > 10 d, or last run failed | weekly Sunday 09:00 + 3 days grace | `DrDrillStale`, selfcheck |
| C: free space | < 15 % for 10 min | VHDX growth can fill C: and stop Docker + native Postgres together | `HostDiskFreeLow`, selfcheck |
| `docker_data.vhdx` size | > 1000 GB | internal used is ~660 GB; the rest is uncompacted slack | `DockerVhdxLarge` |
| GPU free VRAM | < 400 MiB for 30 min | measured steady state is 0.7-1.0 GiB free on the 32 GiB card | `GpuVramNearlyFull`, selfcheck |
| GPU pegged, no tokens (spinner) | util > 95% for 15m and qwen38-chat < 1 token/s | 2026-09-21 wedge signature | `GpuSpinner` |
| GPU VRAM used ratio | > 97% for 15m | after the GPU plan (chat GPU_UTIL 0.70) steady state is ~90-93% | `GpuVramSustainedHigh`, selfcheck |
| Embedding p95 via Bifrost | > 1s for 15m | after cpu_shares: typical < 0.2s, < 2s under full load | `EmbeddingLatencySloBreach`, `GpuBudgetProbeFailing`, selfcheck |
| Local chat lane | waiting > 2 for 10m, or genuine errors > 5% for 15m | MAX_SEQS 32, KV usage measured 5-18% | `LocalChatLaneQueueing`, `LocalChatLaneErrors` |
| Container restarts | > 3 in 1 h | crash loop, not a deploy | `ContainerRestartLoop` |
| Container health | any unhealthy for 10 min | | `ContainersUnhealthy`, selfcheck |
| Expected containers | any of `scripts/ops_expected_containers.json` absent 5 min | INF-01: `legion-db-backup` vanished and nothing noticed | `ExpectedContainerMissing`, selfcheck |
| hostcron job | 2 consecutive failures | one flake is noise, two is a broken job | `HostcronJobFailing`, selfcheck |
| Non-loopback listeners | any port not in `scripts/ops_exposure_allowlist.json` | unauthenticated services must be loopback-only | `UnexpectedExposedPort`, selfcheck |
| Self-check silence | no report in 36 h | the watcher watches itself | `OpsSelfCheckSilent` |

## VRAM budget (RTX 5090, 32,607 MiB)

The card runs at ~96 % by design: `qwen38-chat` (27B, fp8 KV, long context), `vllm-embed`, the
reranker, Kronos/Chronos forecasters, and Windows desktop apps (browsers hold several GiB of WDDM
memory). Lowering `gpu_memory_utilization` on `qwen38-chat` would cut its context/KV and slow every
LLM lane, so the response to low free VRAM is to reclaim desktop-app VRAM or move a tenant, not to
retune the engine reflexively. The alert fires at 400 MiB free so a real OOM risk pages while the
normal 0.7-1.0 GiB operating band does not.

## Disk reclaim ladder (never deletes volumes)

1. `docker image prune -f` (dangling only) and `docker builder prune -f` (build cache).
2. Rotated container logs are capped per service (`json-file` `max-size`/`max-file`).
3. `scripts/compact-docker-disk.ps1` (elevated; stops Docker) returns the VHDX slack to NTFS
   (VHDX 1.04 TB vs ~660 GB used inside on 2026-09-30).

| Host available memory | < 4 GiB for 15 min (`HostMemoryLow`, probe `ops-hostmem` every 15 min) | 200 MB available measured 2026-09-30 with ADA latency degraded; host 64 GB, WSL capped 32 GB, Postgres ~8.5 GB |
