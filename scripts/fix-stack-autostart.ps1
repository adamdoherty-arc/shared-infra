<#
.SYNOPSIS
    Give ADA, Zero and ERPNext the same boot-time autostart service that
    shared-infra and Legion already have.

.DESCRIPTION
    Measured live 2026-09-22: Docker Desktop restarted at ~15:35 ET and
    twenty containers stayed dead for two hours with nothing noticing --
    the whole Zero stack (9) and the whole ERPNext stack (11). ADA's own
    14 containers were only back because interactive sessions had
    restarted them by hand during the same window.

    Root cause is the one already documented for the SearXNG outage
    earlier the same day: every container in every stack uses
    restart: unless-stopped, and unless-stopped deliberately does NOT
    restore a container that was stopped explicitly -- which is exactly
    what "docker desktop stop" (and a Docker Desktop settings change, and
    a host reboot) does. The only thing that brings a stack back is a
    boot-time service that re-composes it, and only two of the five
    stacks had one:

        SharedInfra-Stack  -> C:\code\shared-infra   (7 compose files)
        Legion-Stack       -> C:\code\Legion         (docker-compose.yml)

    ADA, Zero and ERPNext had none. This installs them, mirroring the
    working services exactly (nssm, LocalSystem, AUTO_START, the same
    docker.exe, "compose ... up" with no -d so the service supervises).

    Docker Desktop itself must already be set to start at login, or no
    service here can reach a daemon; the script checks and says so.

.PARAMETER DryRun
    Print what would change and exit. Needs no elevation.

.PARAMETER Revert
    Remove the three services this script installs. Leaves the two
    pre-existing services (SharedInfra-Stack, Legion-Stack) untouched.

.PARAMETER NoStart
    Install the services but do not start them now.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\code\shared-infra\scripts\fix-stack-autostart.ps1 -DryRun
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\code\shared-infra\scripts\fix-stack-autostart.ps1
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Revert,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

$Nssm   = 'C:\ProgramData\chocolatey\lib\NSSM\tools\nssm.exe'
$Docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'

# Mirrors the two services that already work. "up" without -d is
# deliberate: nssm supervises the foreground compose process, which is how
# SharedInfra-Stack and Legion-Stack are configured today.
$Stacks = @(
    @{ Service = 'ADA-Stack';     Dir = 'C:\code\ADA';     Compose = 'docker-compose.yml' },
    @{ Service = 'Zero-Stack';    Dir = 'C:\code\zero';    Compose = 'docker-compose.sprint.yml' },
    @{ Service = 'ERPNext-Stack'; Dir = 'C:\code\erpnext'; Compose = 'docker-compose.yml' }
)
foreach ($s in $Stacks) { $s.Params = "compose -f $($s.Compose) up" }

function Write-Step { param($m) Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "  $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  $m" -ForegroundColor Yellow }
function Write-Bad  { param($m) Write-Host "  $m" -ForegroundColor Red }

function Test-ServiceExists {
    param($n)
    return $null -ne (Get-Service -Name $n -ErrorAction SilentlyContinue)
}

function Get-NssmValue {
    param($Service, $Key)
    $raw = & $Nssm get $Service $Key 2>&1
    return (($raw -join '') -replace "`0", '').Trim()
}

# --- preflight (no elevation needed) ---------------------------------------
Write-Step "Preflight"
$fatal = $false
if (-not (Test-Path $Nssm))   { Write-Bad "nssm not found at $Nssm";     $fatal = $true } else { Write-Ok "nssm   : $Nssm" }
if (-not (Test-Path $Docker)) { Write-Bad "docker not found at $Docker"; $fatal = $true } else { Write-Ok "docker : $Docker" }

foreach ($s in $Stacks) {
    $composePath = Join-Path $s.Dir $s.Compose
    if (-not (Test-Path $s.Dir)) {
        Write-Bad "missing dir  : $($s.Dir)"
        $fatal = $true
    }
    elseif (-not (Test-Path $composePath)) {
        Write-Bad "missing file : $composePath"
        $fatal = $true
    }
    else {
        if (Test-ServiceExists $s.Service) { $state = 'EXISTS (will be reconfigured)' }
        else { $state = 'absent (will be created)' }
        Write-Ok "$($s.Service.PadRight(14)) -> $composePath  [$state]"
    }
}
if ($fatal) { Write-Bad "Preflight failed -- nothing changed."; exit 1 }

# Every service here is useless if the daemon itself does not come back.
$ddAuto = $false
try {
    $run = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
    if ($run -and ($run.PSObject.Properties.Name -contains 'Docker Desktop')) { $ddAuto = $true }
} catch { }
if ($ddAuto) {
    Write-Ok "Docker Desktop autostart: detected"
} else {
    Write-Warn "Docker Desktop autostart NOT detected -- enable Settings > General >"
    Write-Warn "  'Start Docker Desktop when you sign in', or these services have no"
    Write-Warn "  daemon to talk to on a cold boot."
}

if ($DryRun) {
    Write-Step "DryRun -- nothing installed, nothing started"
    foreach ($s in $Stacks) {
        Write-Host "  $($s.Service)"
        Write-Host "    Application   = $Docker"
        Write-Host "    AppDirectory  = $($s.Dir)"
        Write-Host "    AppParameters = $($s.Params)"
    }
    exit 0
}

# --- self-elevate (service install writes HKLM) ----------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin  = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
                [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warn "Not elevated. Relaunching with UAC -- approve the prompt."
    $argList = @('-ExecutionPolicy', 'Bypass', '-NoProfile', '-NoExit', '-File', "`"$PSCommandPath`"")
    if ($Revert)  { $argList += '-Revert' }
    if ($NoStart) { $argList += '-NoStart' }
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs
    } catch {
        Write-Bad "Elevation refused. Right-click PowerShell -> Run as administrator, then re-run."
        exit 1
    }
    exit 0
}

# --- revert ----------------------------------------------------------------
if ($Revert) {
    Write-Step "Reverting -- removing the three services this script installs"
    foreach ($s in $Stacks) {
        if (Test-ServiceExists $s.Service) {
            & $Nssm stop   $s.Service confirm 2>&1 | Out-Null
            & $Nssm remove $s.Service confirm 2>&1 | Out-Null
            Write-Ok "removed $($s.Service)"
        } else {
            Write-Warn "$($s.Service) not present -- nothing to remove"
        }
    }
    Write-Ok "SharedInfra-Stack and Legion-Stack were not touched."
    exit 0
}

# --- install / reconfigure (idempotent) ------------------------------------
Write-Step "Installing services"
foreach ($s in $Stacks) {
    if (-not (Test-ServiceExists $s.Service)) {
        & $Nssm install $s.Service $Docker 2>&1 | Out-Null
        Write-Ok "created $($s.Service)"
    } else {
        Write-Warn "$($s.Service) exists -- reconfiguring in place"
    }
    & $Nssm set $s.Service Application   $Docker   2>&1 | Out-Null
    & $Nssm set $s.Service AppDirectory  $s.Dir    2>&1 | Out-Null
    & $Nssm set $s.Service AppParameters $s.Params 2>&1 | Out-Null
    & $Nssm set $s.Service Start SERVICE_AUTO_START 2>&1 | Out-Null
    & $Nssm set $s.Service AppExit Default Restart  2>&1 | Out-Null
}

# --- verify the write actually took ----------------------------------------
Write-Step "Verifying configuration"
$bad = $false
foreach ($s in $Stacks) {
    $app = Get-NssmValue $s.Service 'Application'
    $dir = Get-NssmValue $s.Service 'AppDirectory'
    $par = Get-NssmValue $s.Service 'AppParameters'
    if ($app -eq $Docker -and $dir -eq $s.Dir -and $par -eq $s.Params) {
        Write-Ok "$($s.Service): OK"
    } else {
        Write-Bad "$($s.Service): MISMATCH"
        Write-Bad "  Application   = $app"
        Write-Bad "  AppDirectory  = $dir"
        Write-Bad "  AppParameters = $par"
        $bad = $true
    }
}
if ($bad) { Write-Bad "At least one service did not take the configuration."; exit 1 }

if ($NoStart) {
    Write-Step "NoStart set -- services installed but not started"
    exit 0
}

# --- start ------------------------------------------------------------------
Write-Step "Starting services"
foreach ($s in $Stacks) {
    try {
        Start-Service -Name $s.Service -ErrorAction Stop
        Write-Ok "started $($s.Service)"
    } catch {
        # A stack already composed up by hand is the normal case here; the
        # service is still correctly registered for the next cold boot.
        Write-Warn "$($s.Service) did not start now: $($_.Exception.Message)"
        Write-Warn "  (already-running stacks are expected; registered for next boot)"
    }
}

Write-Step "Result"
Get-Service | Where-Object { $_.Name -match 'Stack$' } |
    Select-Object Name, Status, StartType | Format-Table -AutoSize | Out-String | Write-Host
Write-Host "All five stacks now have a boot-time service. Nothing here is destructive;" -ForegroundColor Gray
Write-Host "re-run with -Revert to remove the three this script added." -ForegroundColor Gray
