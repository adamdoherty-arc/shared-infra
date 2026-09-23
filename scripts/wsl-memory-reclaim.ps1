<#
.SYNOPSIS
  Forces the docker-desktop WSL2 VM to release reclaimable page cache back to
  the Windows host when host memory gets tight. Only acts above a threshold --
  this is NOT a periodic no-op churn script.

  Origin: 2026-09-16. Docker Desktop restarted after a networking failure
  (missing vEthernet(WSL) adapter) and host RAM hit ~98% used / <1GB
  available with sustained >2000 pages/sec paging -- the same failure class
  documented in ADA's docs/ADA_MASTER_PLAN.md section 42 (2026-09-14 CC
  scanner incident, "48 containers restarted together at 13:45 ET when the
  WSL VM went down"). A manual `wsl -d docker-desktop -- sh -c "sync; echo 1
  > /proc/sys/vm/drop_caches"` recovered available memory from 337MB to
  12.4GB within 20s with no data loss (page cache only, post-sync). WSL2's
  own `autoMemoryReclaim=gradual` (set in .wslconfig) is too slow to relieve
  an acute crunch on its own. This script automates that manual recovery so
  it no longer requires a human/agent to notice and intervene.

  Full incident + rationale: Legion product_feature:shared-infra note 8291.
  ADA's ada-master fix_playbook.md recipes #33/#34 document the manual
  diagnosis path this script now automates recipe #34 of.

.NOTES
  Registered as scheduled task "Shared Infra - WSL Memory Reclaim", every 20
  min, via install (see bottom of this file's companion install step). Safe
  and idempotent: a no-op when memory is healthy, and drop_caches only
  discards clean (already-synced) page cache -- never dirty/unwritten data.
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

# wsl.exe writes its list output as UTF-16LE. Without WSL_UTF8=1, PowerShell's
# console decode (UTF-8 here) mangles "docker-desktop" into "d o c k e r - d
# e s k t o p" with embedded nulls rendered as spaces, so the string match
# below silently NEVER matches -- this made the entire task a no-op logger
# for its whole life (2026-09-16 install through 2026-09-17 06:21, ~40+ runs,
# every single one hit this skip branch even while host memory was down to
# 567MB available / 9,085 pages/sec). Found + fixed 2026-09-17 during the
# ada-master Daily ring recurrence investigation (Legion note 8291/8292
# follow-up). Verified live: with WSL_UTF8=1 the same `wsl -l -v` call
# renders normal ASCII and the match succeeds.
$env:WSL_UTF8 = "1"
$wslRunning = (wsl -l -v 2>&1 | Select-String "docker-desktop" | Select-String "Running")
if (-not $wslRunning) {
    Write-Log "docker-desktop WSL distro not running -- nothing to reclaim, skipping"
    exit 0
}

wsl -d docker-desktop -- sh -c "sync; echo 1 > /proc/sys/vm/drop_caches" 2>&1 | Out-Null

Start-Sleep -Seconds 5
$availAfter = (Get-Counter '\Memory\Available MBytes').CounterSamples.CookedValue
$pagesAfter = (Get-Counter '\Memory\Pages/sec').CounterSamples.CookedValue

Write-Log "reclaimed: available ${availBefore}MB -> ${availAfter}MB, pages/sec ${pagesBefore} -> ${pagesAfter}"
