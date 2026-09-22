<#
.SYNOPSIS
    Repoint the SharedInfra-Stack boot service at every shared-infra compose
    file, not just the vLLM one.

.DESCRIPTION
    Measured 2026-09-22. The nssm service SharedInfra-Stack runs:

        docker.exe compose -f docker-compose.vllm.yml up     (AppDirectory C:\code\shared-infra)

    so only the vLLM stack is brought back by the machine. Bifrost, SearXNG,
    the observability stack, freellmapi and the control plane rely entirely on
    `restart: unless-stopped`, which does NOT restore a container that was
    stopped by an explicit `docker desktop stop`.

    Consequence, observed twice today: a Docker Desktop stop at 15:01:40 UTC
    left shared-searxng, shared-freellmapi, shared-grafana, shared-prometheus,
    shared-alertmanager, shared-tempo, loki, otelcol, cadvisor and
    dcgm-exporter Exited for over an hour. The only thing that noticed was
    ADA's provider-health alert, which reached the owner as a CRITICAL
    "Searxng API failing" on the System Hub.

    The merged file set below was validated with `docker compose config` and
    converges idempotently (18 services, all reported Running, nothing
    recreated).

    Writing to HKLM\SYSTEM\CurrentControlSet\Services requires elevation, so
    this script self-elevates via UAC.

.PARAMETER Revert
    Restore the original single-file parameters.

.PARAMETER DryRun
    Report what would change and validate the merged compose set. Needs no
    elevation, writes nothing.

.PARAMETER NoRestart
    Change the service configuration but do not restart the service. The new
    parameters then take effect at the next boot or manual service restart.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\code\shared-infra\scripts\fix-infra-autostart.ps1
#>
[CmdletBinding()]
param(
    [switch]$Revert,
    [switch]$NoRestart,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$ServiceName = 'SharedInfra-Stack'
$RegPath     = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
$InfraDir    = 'C:\code\shared-infra'

$ComposeFiles = @(
    'docker-compose.vllm.yml'
    'docker-compose.bifrost.yml'
    'docker-compose.searxng.yml'
    'docker-compose.observability.yml'
    'docker-compose.otelcol.yml'
    'docker-compose.freellmapi.yml'
    'docker-compose.control.yml'
)

$NewParams = 'compose ' + (($ComposeFiles | ForEach-Object { "-f $_" }) -join ' ') + ' up'
$OldParams = 'compose -f docker-compose.vllm.yml up'

function Write-Step { param($m) Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "  $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  $m" -ForegroundColor Yellow }

# --- self-elevate -----------------------------------------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin  = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
                [Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin -and -not $DryRun) {
    Write-Warn 'Not elevated. Relaunching with UAC -- approve the prompt.'
    $argList = @('-ExecutionPolicy','Bypass','-NoProfile','-NoExit','-File',"`"$PSCommandPath`"")
    if ($Revert)    { $argList += '-Revert' }
    if ($NoRestart) { $argList += '-NoRestart' }
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs
    } catch {
        Write-Host 'Elevation refused. Right-click PowerShell -> Run as administrator, then re-run.' -ForegroundColor Red
        exit 1
    }
    exit 0
}

# --- preflight --------------------------------------------------------------
Write-Step 'Preflight'

if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    Write-Host "Service $ServiceName not found." -ForegroundColor Red; exit 1
}

$current = (Get-ItemProperty -Path $RegPath -Name 'AppParameters').AppParameters
Write-Ok "service   : $ServiceName ($((Get-Service $ServiceName).Status))"
Write-Ok "current   : $current"

$target = if ($Revert) { $OldParams } else { $NewParams }
Write-Ok "target    : $target"

if ($current -eq $target) {
    Write-Step 'Already configured -- nothing to do'
    exit 0
}

foreach ($f in $ComposeFiles) {
    $p = Join-Path $InfraDir $f
    if (-not (Test-Path $p)) { Write-Host "Missing compose file: $p" -ForegroundColor Red; exit 1 }
}
Write-Ok "all $($ComposeFiles.Count) compose files present"

if (-not $Revert) {
    Write-Step 'Validating the merged compose set'
    Push-Location $InfraDir
    try {
        $cfgArgs = @('compose') + ($ComposeFiles | ForEach-Object { '-f'; $_ }) + @('config','--quiet')
        & docker.exe @cfgArgs
        if ($LASTEXITCODE -ne 0) { throw "docker compose config failed with exit $LASTEXITCODE" }
        Write-Ok 'merged compose config is valid'
    } catch {
        Write-Host "Validation failed, not touching the service: $($_.Exception.Message)" -ForegroundColor Red
        Pop-Location; exit 1
    }
    Pop-Location
}

if ($DryRun) {
    Write-Step 'DryRun -- nothing written, service untouched'
    Write-Ok "would set AppParameters to: $target"
    exit 0
}

# --- apply ------------------------------------------------------------------
Write-Step 'Updating service parameters'
Set-ItemProperty -Path $RegPath -Name 'AppParameters' -Value $target
$verify = (Get-ItemProperty -Path $RegPath -Name 'AppParameters').AppParameters
if ($verify -ne $target) { Write-Host 'Write did not stick.' -ForegroundColor Red; exit 1 }
Write-Ok 'parameters updated and read back'

if ($NoRestart) {
    Write-Step 'NoRestart set'
    Write-Ok 'Takes effect at the next boot or manual service restart.'
    exit 0
}

Write-Step 'Restarting the service so the new set is supervised now'
Write-Warn 'This cycles the shared-infra stack, including the LLM gateway and engine.'
Restart-Service -Name $ServiceName -Force
Start-Sleep -Seconds 30
Write-Ok "service status: $((Get-Service $ServiceName).Status)"

# --- verify -----------------------------------------------------------------
Write-Step 'Verification'
$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 15
    $running = @(& docker.exe ps --filter 'label=com.docker.compose.project=shared-infra' --format '{{.Names}}').Count
    Write-Ok "shared-infra containers running: $running"
} until ($running -ge 17 -or (Get-Date) -gt $deadline)

& docker.exe ps -a --filter 'label=com.docker.compose.project=shared-infra' --format '{{.Names}}|{{.Status}}' |
    Sort-Object | Out-String | Write-Host

$code = try {
    (Invoke-WebRequest -Uri 'http://127.0.0.1:4445/v1/models' -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop).StatusCode
} catch { $_.Exception.Response.StatusCode.value__ }
Write-Ok "bifrost /v1/models: HTTP $code (200 or 401 is healthy)"

$sx = try {
    (Invoke-WebRequest -Uri 'http://127.0.0.1:8091/healthz' -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop).StatusCode
} catch { $_.Exception.Response.StatusCode.value__ }
Write-Ok "searxng /healthz  : HTTP $sx"

Write-Step 'Done'
Write-Host "`nRevert at any time with:  -Revert" -ForegroundColor Gray
