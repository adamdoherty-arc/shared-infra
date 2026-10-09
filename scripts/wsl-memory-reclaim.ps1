<#
.SYNOPSIS
  Trims idle desktop-tool working sets (never the docker-desktop VM) so the
  Windows host gets memory back when it gets tight. Only acts below a
  threshold -- this is NOT a periodic no-op churn script.

  Origin: 2026-09-16. Docker Desktop restarted after a networking failure
  (missing vEthernet(WSL) adapter) and host RAM hit ~98% used / <1GB
  available with sustained >2000 pages/sec paging -- the same failure class
  documented in ADA's docs/ADA_MASTER_PLAN.md section 42 (2026-09-14 CC
  scanner incident, "48 containers restarted together at 13:45 ET when the
  WSL VM went down").

  2026-10-09: this script no longer touches the docker-desktop VM at all. Until
  then it ran a page-cache drop inside the VM on every trigger (14 of 31 runs
  on 2026-10-09 alone, every 20 minutes while host memory sat under 6GB). The
  same VM cache drop cut every container off host.docker.internal:5432 for 30
  minutes on 2026-10-06 (630 exceptions in 24h), and the 2026-10-09 System Hub
  item exceptions_24h (121, red at 100) showed the same "timeout expired" /
  "No route to host" bursts on the 20-minute cadence. WSL's own
  autoMemoryReclaim=gradual (.wslconfig [experimental]) is the VM-side reclaim.
  Gate: scripts/tests/test_wsl_memory_reclaim.py.

  Full incident + rationale: Legion product_feature:shared-infra note 8291.

.NOTES
  Registered as scheduled task "Shared Infra - WSL Memory Reclaim", every 20
  min. Safe and idempotent: a no-op when memory is healthy.
#>

param(
    [double]$AvailableMbThreshold = 6144,   # act when available memory drops below 6GB
    [string]$LogPath = "$env:USERPROFILE\.claude\logs\wsl-memory-reclaim.log"
)

function Write-Log {
    param([string]$Message)
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $Message
    Add-Content -Path $LogPath -Value $line
}

$logDir = Split-Path -Parent $LogPath
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

$availBefore = (Get-Counter '\Memory\Available MBytes').CounterSamples.CookedValue
$pagesBefore = (Get-Counter '\Memory\Pages/sec').CounterSamples.CookedValue

if ($availBefore -ge $AvailableMbThreshold) {
    Write-Log "skip: available=${availBefore}MB >= threshold=${AvailableMbThreshold}MB, pages/sec=$pagesBefore"
    exit 0
}

Write-Log "triggering: available=${availBefore}MB < threshold=${AvailableMbThreshold}MB, pages/sec=$pagesBefore"

# 2026-10-01: idle desktop-tool working sets (codegraph/MCP node, claude sessions, Cursor,
# chrome) are trimmed: EmptyWorkingSet only moves pages to the standby/compressed lists; the
# owning process re-faults what it uses. Measured +3GB. Postgres, vmmemWSL, Docker backend and
# thinkorswim are deliberately NOT trimmed.
Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class WsTrim{[DllImport("psapi.dll")]public static extern bool EmptyWorkingSet(IntPtr h);}' -ErrorAction SilentlyContinue
foreach ($p in Get-Process -Name node,claude,Cursor,chrome,chrome-headless-shell,Discord,python -ErrorAction SilentlyContinue) {
    try { [void][WsTrim]::EmptyWorkingSet($p.Handle) } catch {}
}

Start-Sleep -Seconds 5
$availAfter = (Get-Counter '\Memory\Available MBytes').CounterSamples.CookedValue
$pagesAfter = (Get-Counter '\Memory\Pages/sec').CounterSamples.CookedValue

Write-Log "reclaimed: available ${availBefore}MB -> ${availAfter}MB, pages/sec ${pagesBefore} -> ${pagesAfter}"
