# Run by the ZeroInfra-DockerGuiReclaim scheduled task (AtLogOn +45 s, Interactive) through the
# C:\tmp\reclaim-docker-gui.ps1 shim. The task name and shim path predate Zero's retirement; repointing the
# task needs elevation, so the shim forwards here and this file is the versioned source.
#
# Docker Desktop launched from session 0 by the boot task renders its GUI where the operator cannot see it, and
# its single-instance mutex makes the operator's own logon copy hand off and exit. Only a restart moves the GUI.
#
# 2026-10-09 incident: this restart (`docker desktop stop` at 12:34:58Z) stopped every container through the API,
# which marks them manually stopped, so the daemon restored none of the unless-stopped ones and only the four
# compose stacks below came back. ada-bitcoin-prod (compose profile), ada-nginx (plain docker run), ERPNext,
# customer-ops and fortressos stayed Exited ~8 minutes. Now:
#   * an invisible GUI is cosmetic, live money is not: when the engine is serving and ADA reports live Kalshi
#     exposure (or the check cannot run), the GUI stays in session 0 and nothing is restarted;
#   * before a restart the running container set is snapshotted and every one of them is docker-started after
#     the engine returns, then ops_reconcile.py --restore-only sweeps anything left;
#   * every step is logged to scripts\hostcron\logs\docker-boot-tasks.log.
$ErrorActionPreference = 'SilentlyContinue'
$dd = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$py = 'C:\Python314\python.exe'
$si = 'C:\code\shared-infra'
$log = "$si\scripts\hostcron\logs\docker-boot-tasks.log"
$snapshot = "$si\state\docker-gui-reclaim-running.txt"
$mySession = (Get-Process -Id $PID).SessionId
function Log([string]$m) { Add-Content -Path $log -Value ("{0} gui-reclaim {1}" -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $m) }
function Get-Gui { Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue }
function Engine-Up {
  $null = & docker version --format '{{.Server.Version}}' 2>$null
  return $LASTEXITCODE -eq 0
}
function Wait-Engine([int]$seconds = 300) {
  for ($i = 0; $i -lt [math]::Ceiling($seconds / 10); $i++) {
    if (Engine-Up) { return $true }
    Start-Sleep -Seconds 10
  }
  return $false
}
function Exposure-Blockers {
  $code = "import json,sys;sys.path.insert(0,r'$si\scripts');import ops_reconcile as o;print(json.dumps(o.live_exposure_blockers()))"
  $out = & $py -c $code 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $out) { return @('exposure check could not run') }
  $parsed = ($out | Select-Object -Last 1) | ConvertFrom-Json
  return @($parsed)
}

$guiOk = $false
for ($i = 0; $i -lt 12; $i++) {
  if (Get-Gui | Where-Object { $_.SessionId -eq $mySession }) { $guiOk = $true; break }
  Start-Sleep -Seconds 5
}

if ($guiOk) {
  Log "GUI already in session $mySession - no restart needed"
} else {
  $all = Get-Gui
  if (-not $all) {
    Log 'Docker Desktop not running at all - starting it in this session'
    Start-Service com.docker.service -ErrorAction SilentlyContinue
    Start-Process $dd
  } else {
    $sessions = ($all | Select-Object -ExpandProperty SessionId -Unique) -join ','
    $blockers = @()
    if (Engine-Up) { $blockers = @(Exposure-Blockers | Where-Object { $_ }) }
    if ($blockers.Count -gt 0) {
      Log ("GUI stranded in session(s) $sessions but NOT restarting (live exposure): " + ($blockers -join '; '))
    } else {
      $running = @(& docker ps --format '{{.Names}}' 2>$null | Where-Object { $_ })
      New-Item -ItemType Directory -Force -Path (Split-Path $snapshot) | Out-Null
      Set-Content -Path $snapshot -Value $running -Encoding utf8
      Log "GUI stranded in session(s) $sessions (mine=$mySession) - restarting; snapshot of $($running.Count) running containers"
      & docker desktop stop 2>&1 | Out-Null
      for ($i = 0; $i -lt 12; $i++) { if (-not (Get-Gui)) { break }; Start-Sleep -Seconds 5 }
      Start-Service com.docker.service -ErrorAction SilentlyContinue
      Start-Process $dd
    }
  }
}

if (-not (Wait-Engine 300)) { Log 'engine did NOT come up within 300s'; exit 1 }
Log 'engine up'

$stacks = @(
  @{ Name = 'ADA';     File = 'C:\code\ADA\docker-compose.yml';                      Dir = 'C:\code\ADA' },
  @{ Name = 'Legion';  File = 'C:\code\legion\docker-compose.yml';                   Dir = 'C:\code\legion' },
  @{ Name = 'vLLM';    File = 'C:\code\shared-infra\docker-compose.vllm.yml';        Dir = 'C:\code\shared-infra' },
  @{ Name = 'Bifrost'; File = 'C:\code\shared-infra\docker-compose.bifrost.yml';     Dir = 'C:\code\shared-infra' }
)
foreach ($s in $stacks) {
  if (-not (Test-Path $s.File)) { Log "$($s.Name): compose file missing, skipped"; continue }
  & docker compose -f $s.File --project-directory $s.Dir up -d 2>&1 | Out-Null
  Log "$($s.Name): up -d issued (exit $LASTEXITCODE)"
}

if (Test-Path $snapshot) {
  $want = @(Get-Content $snapshot | Where-Object { $_ })
  $now = @(& docker ps --format '{{.Names}}' 2>$null)
  $missing = @($want | Where-Object { $now -notcontains $_ })
  foreach ($n in $missing) {
    & docker start $n 2>&1 | Out-Null
    Log "snapshot restore: docker start $n (exit $LASTEXITCODE)"
  }
  Remove-Item $snapshot -Force
}
$sweep = & $py "$si\scripts\ops_reconcile.py" --restore-only 2>&1
Log ("ops_reconcile --restore-only: " + (($sweep | Select-Object -Last 1) -as [string]))
Log 'reclaim complete'
exit 0
