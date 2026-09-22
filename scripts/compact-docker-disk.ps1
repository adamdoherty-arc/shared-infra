<#
.SYNOPSIS
    Compact the Docker Desktop WSL2 data disk, then bring every stack back up.

.DESCRIPTION
    The docker_data.vhdx grows and never shrinks on its own. Measured 2026-09-22:
    968 GB allocated against 676 GB actually used, i.e. ~290 GB of reclaimable slack.

    diskpart's `compact vdisk` is the only safe reclaim path on this host:
      - Optimize-VHD needs the Hyper-V module, absent on Windows 11 Home.
      - `wsl --manage --set-sparse true` refuses without --allow-unsafe, which
        Microsoft warns can corrupt data. This disk holds Legion's and Zero's
        Postgres volumes, ~142 GB of ADA backups and ~97 GB of model weights,
        so that flag is not an option here.

    `diskpart` requires elevation, so this script self-elevates via UAC.

    IMPORTANT, learned the hard way on 2026-09-22: `docker desktop stop` leaves
    containers in Exited state -- they do NOT come back by restart policy when the
    daemon returns. The stacks must be explicitly composed up afterwards, in
    dependency order, which is what -RestoreStacks does.

.PARAMETER DryRun
    Measure and report only. Stops nothing, compacts nothing.

.PARAMETER SkipRestore
    Compact, start Docker, but do not compose the stacks back up.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\code\shared-infra\scripts\compact-docker-disk.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\compact-docker-disk.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipRestore
)

$ErrorActionPreference = 'Stop'

$VhdxPath   = "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx"
$Sidecars   = @(
    'ada-backend-autoheal','ada-scheduler-autoheal','bifrost-autoheal',
    'vllm-autoheal','vllm-wedge-monitor','ada-backend-restart-auditor'
)
# Ordered: gateway and engine first, then ADA, then the rest.
$Stacks = @(
    @{ Name='bifrost';       Dir='C:\code\shared-infra'; File='docker-compose.bifrost.yml' },
    @{ Name='vllm';          Dir='C:\code\shared-infra'; File='docker-compose.vllm.yml' },
    @{ Name='observability'; Dir='C:\code\shared-infra'; File='docker-compose.observability.yml' },
    @{ Name='freellmapi';    Dir='C:\code\shared-infra'; File='docker-compose.freellmapi.yml' },
    @{ Name='searxng';       Dir='C:\code\shared-infra'; File='docker-compose.searxng.yml' },
    @{ Name='ada';           Dir='C:\code\ADA';          File='docker-compose.yml' },
    @{ Name='legion';        Dir='C:\code\legion';       File='docker-compose.yml' },
    @{ Name='zero';          Dir='C:\code\zero';         File='docker-compose.sprint.yml' },
    @{ Name='erpnext';       Dir='C:\code\erpnext';      File='docker-compose.yml' }
)

function Write-Step { param($m) Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "  $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  $m" -ForegroundColor Yellow }
function Get-FreeGB { [math]::Round((Get-PSDrive C).Free / 1GB, 1) }
function Get-VhdxGB {
    if (Test-Path $VhdxPath) { [math]::Round((Get-Item $VhdxPath).Length / 1GB, 1) } else { 0 }
}

# --- self-elevate -----------------------------------------------------------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin   = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
                [Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin -and -not $DryRun) {
    Write-Warn "Not elevated. Relaunching with UAC -- approve the prompt."
    $argList = @('-ExecutionPolicy','Bypass','-NoProfile','-NoExit','-File',"`"$PSCommandPath`"")
    if ($SkipRestore) { $argList += '-SkipRestore' }
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs
    } catch {
        Write-Host "Elevation refused. Right-click PowerShell -> Run as administrator, then re-run." -ForegroundColor Red
        exit 1
    }
    exit 0
}

# --- preflight --------------------------------------------------------------
Write-Step "Preflight"
if (-not (Test-Path $VhdxPath)) { Write-Host "vhdx not found at $VhdxPath" -ForegroundColor Red; exit 1 }

$sizeBefore = Get-VhdxGB
$freeBefore = Get-FreeGB
Write-Ok "vhdx      : $sizeBefore GB"
Write-Ok "C: free   : $freeBefore GB"
Write-Ok "elevated  : $isAdmin"

try {
    $usage = & docker run --rm --privileged --pid=host alpine `
        nsenter -t 1 -m -u -n -i -- df -h /var/lib 2>$null | Select-Object -Last 1
    if ($usage) { Write-Ok "in-VM use : $usage" }
} catch { Write-Warn "Could not read in-VM usage (Docker may be down) -- continuing." }

if ($DryRun) {
    Write-Step "DryRun -- nothing stopped, nothing compacted"
    Write-Ok "Reclaimable is roughly (vhdx size) minus (in-VM used) from the lines above."
    exit 0
}

# --- stop ------------------------------------------------------------------
Write-Step "Stopping supervision sidecars first (they cascade-restart on a cold boot)"
foreach ($c in $Sidecars) {
    & docker stop $c 2>$null | Out-Null
    Write-Ok "stopped $c"
}

Write-Step "Stopping Docker Desktop"
& docker desktop stop 2>&1 | Out-String | Write-Host
Start-Sleep -Seconds 20

Write-Step "Shutting down WSL"
& wsl.exe --shutdown
Start-Sleep -Seconds 15
& wsl.exe -l -v 2>&1 | Out-String | Write-Host

$dockerProcs = @(Get-Process | Where-Object { $_.ProcessName -like '*docker*' }).Count
if ($dockerProcs -gt 0) {
    Write-Warn "$dockerProcs docker process(es) still alive; waiting 20s more."
    Start-Sleep -Seconds 20
}

# --- compact ---------------------------------------------------------------
Write-Step "Compacting (this is the long step -- minutes, no progress bar)"
$script = Join-Path $env:TEMP 'compact-docker-disk.txt'
@"
select vdisk file="$VhdxPath"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@ | Set-Content -Path $script -Encoding ASCII

$started = Get-Date
& diskpart.exe /s $script 2>&1 | Out-String | Write-Host
$elapsed = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
Remove-Item $script -ErrorAction SilentlyContinue

$sizeAfter = Get-VhdxGB
Write-Ok "compaction took $elapsed min"
Write-Ok "vhdx $sizeBefore GB -> $sizeAfter GB (reclaimed $([math]::Round($sizeBefore - $sizeAfter,1)) GB)"

if ($sizeAfter -ge $sizeBefore) {
    Write-Warn "No reclaim. Usual causes: diskpart could not attach (file still locked),"
    Write-Warn "or free blocks were never trimmed. Check the diskpart output above."
}

# --- bring back up ---------------------------------------------------------
Write-Step "Starting Docker Desktop"
& docker desktop start 2>&1 | Out-String | Write-Host

$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 10
    $engineUp = $null -ne (& docker ps 2>$null)
} until ($engineUp -or (Get-Date) -gt $deadline)

if (-not $engineUp) { Write-Host "Engine did not come up in 5 min. Investigate before restoring." -ForegroundColor Red; exit 1 }
Write-Ok "engine up"

if ($SkipRestore) {
    Write-Step "SkipRestore set -- containers left as-is"
    Write-Warn "`docker desktop stop` leaves containers Exited; compose them up when ready."
    exit 0
}

Write-Step "Restoring stacks in dependency order"
foreach ($s in $Stacks) {
    $path = Join-Path $s.Dir $s.File
    if (-not (Test-Path $path)) { Write-Warn "skip $($s.Name): $path not found"; continue }
    Push-Location $s.Dir
    try {
        & docker compose -f $s.File up -d 2>&1 | Select-String -Pattern 'Started|Healthy|Error|error' |
            Select-Object -Last 4 | Out-String | Write-Host
        Write-Ok "$($s.Name) composed"
    } catch {
        Write-Warn "$($s.Name) failed: $($_.Exception.Message)"
    } finally { Pop-Location }

    # Bifrost must answer before ADA boots, or ADA's LLM lane starts degraded.
    if ($s.Name -eq 'bifrost') {
        $bfDeadline = (Get-Date).AddMinutes(4)
        do {
            Start-Sleep -Seconds 10
            $code = try {
                (Invoke-WebRequest -Uri 'http://127.0.0.1:4445/v1/models' -TimeoutSec 5 `
                    -UseBasicParsing -ErrorAction Stop).StatusCode
            } catch { $_.Exception.Response.StatusCode.value__ }
        } until ($code -in 200,401 -or (Get-Date) -gt $bfDeadline)
        if ($code -in 200,401) { Write-Ok "bifrost answering ($code)" }
        else { Write-Warn "bifrost not answering yet -- ADA may boot with a degraded LLM lane" }
    }
}

Write-Step "Starting supervision sidecars last"
foreach ($c in $Sidecars) { & docker start $c 2>$null | Out-Null; Write-Ok "started $c" }

# --- verify ----------------------------------------------------------------
Write-Step "Verification"
$running = @(& docker ps -q).Count
Write-Ok "containers running: $running"

$bad = & docker ps --format '{{.Names}}|{{.Status}}' | Select-String -Pattern 'unhealthy|Restarting'
if ($bad) { Write-Warn "needs a look:"; $bad | Out-String | Write-Host } else { Write-Ok "none unhealthy or restarting" }

foreach ($probe in @(
    @{ N='ada';     U='http://127.0.0.1:8006/api/health' },
    @{ N='legion';  U='http://127.0.0.1:8005/api/features?q=bitcoin' },
    @{ N='bifrost'; U='http://127.0.0.1:4445/api/logs?limit=1' }
)) {
    $c = try {
        (Invoke-WebRequest -Uri $probe.U -TimeoutSec 20 -UseBasicParsing -ErrorAction Stop).StatusCode
    } catch { $_.Exception.Response.StatusCode.value__ }
    Write-Ok "$($probe.N): HTTP $c"
}

Write-Step "Done"
Write-Ok "vhdx   $sizeBefore GB -> $sizeAfter GB"
Write-Ok "C: free $freeBefore GB -> $(Get-FreeGB) GB"
Write-Host "`nIf anything is down, compose that stack manually; nothing here is destructive." -ForegroundColor Gray
