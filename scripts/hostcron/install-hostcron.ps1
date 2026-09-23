#requires -Version 5.1
<#
.SYNOPSIS
  Install hostcron as a Windows service and retire the scheduled tasks it replaces.

.DESCRIPTION
  Run ELEVATED. Two halves, in this order:

    1. Register C:\code\shared-infra\scripts\hostcron\hostcron.py as the
       ADA-HostCron service via NSSM, running as LocalSystem in session 0 so it
       can never render a console window, and start it.

    2. DISABLE (never delete) the Windows scheduled tasks it takes over, plus
       the five claude.exe-driven review tasks the operator retired on
       2026-09-23. Disable rather than delete so the original definitions stay
       inspectable and -Revert can put them back in one step.

  Deliberately NOT touched:
    ZeroInfra-DockerGuiReclaim - a logon task that hands the Docker Desktop GUI
    from session 0 to the interactive desktop. A session-0 service cannot do
    that by definition, and it fires once per logon rather than on an interval,
    so it is not part of the flashing-window problem.

.PARAMETER Revert
  Re-enable every task this script disabled and remove the service.

.PARAMETER SkipTasks
  Install and start the service without touching Task Scheduler. Use this to
  run both schedulers side by side for one cycle before committing.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-hostcron.ps1 -SkipTasks
  powershell -ExecutionPolicy Bypass -File .\install-hostcron.ps1
  powershell -ExecutionPolicy Bypass -File .\install-hostcron.ps1 -Revert
#>
[CmdletBinding()]
param(
    [switch]$Revert,
    [switch]$SkipTasks,
    [string]$Python = 'C:\Python314\python.exe',
    [string]$Nssm   = 'C:\ProgramData\chocolatey\bin\nssm.exe'
)
$ErrorActionPreference = 'Stop'

$ServiceName = 'ADA-HostCron'
$Root        = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script      = Join-Path $Root 'hostcron.py'
$LogDir      = Join-Path $Root 'logs'

# Tasks hostcron now owns. Disabled, not deleted.
$MigratedTasks = @(
    'Shared Infra - WSL Memory Reclaim',
    'ADA Restart Window - Hourly',
    'ADA Master - Pulse',
    'ADA Master - Watchdog',
    'ADA Ratchet Sweep - Nightly',
    'ADA CI-Replacement Gate - Nightly',
    'ADA Comment Burndown - Nightly',
    'ADA Security Sweep - Daily',
    'ADA Security Sweep - Weekly',
    'ADA Bifrost Model Sync - Daily',
    'ADA DR Restore Drill - Weekly',
    'Claude Usage Forensics - Weekly',
    'ADA LLM Weekly Report',
    'ADA Prompt Weekly Review'
)

# Retired outright: each shells out to headless claude.exe. Their capability is
# being rebuilt natively (see docs/native-review-replacement.md). Not migrated,
# because rehoming an unattended code-writing agent is not the goal.
$RetiredClaudeTasks = @(
    'ADA Master - Daily',
    'ADA Master - Weekly',
    'ADA Master - Monthly',
    'A-finance Daily Review',
    'ADA Financial Repos - Weekly'
)

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Must run elevated: service registration and root-folder task edits both require admin.'
    }
}

Assert-Elevated

if ($Revert) {
    Write-Host '== reverting =='
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        & $Nssm stop $ServiceName confirm 2>&1 | Out-Null
        & $Nssm remove $ServiceName confirm 2>&1 | Out-Null
        Write-Host "removed service $ServiceName"
    }
    foreach ($t in ($MigratedTasks + $RetiredClaudeTasks)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Enable-ScheduledTask -TaskName $t | Out-Null
            Write-Host "re-enabled: $t"
        }
    }
    Write-Host 'revert complete.'
    return
}

# ---------------------------------------------------------------- preflight
foreach ($p in @($Python, $Nssm, $Script)) {
    if (-not (Test-Path $p)) { throw "missing prerequisite: $p" }
}
& $Python -c "import ast,io,sys; ast.parse(io.open(sys.argv[1],encoding='utf-8').read())" $Script
if ($LASTEXITCODE -ne 0) { throw 'hostcron.py failed to parse - refusing to install' }

$sched = Join-Path $Root 'schedule.json'
$jobCount = (& $Python -c "import json,io,sys; d=json.load(io.open(sys.argv[1],encoding='utf-8')); print(sum(1 for j in d['jobs'] if j.get('enabled',True)))" $sched)
if ($LASTEXITCODE -ne 0) { throw 'schedule.json is not valid JSON - refusing to install' }
Write-Host "preflight OK - $jobCount enabled jobs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ------------------------------------------------------------- the service
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "service exists; stopping for reinstall"
    & $Nssm stop $ServiceName confirm 2>&1 | Out-Null
    & $Nssm remove $ServiceName confirm 2>&1 | Out-Null
    Start-Sleep -Seconds 2
}

& $Nssm install $ServiceName $Python $Script
& $Nssm set $ServiceName AppDirectory     $Root
& $Nssm set $ServiceName DisplayName      'ADA hostcron (host-side scheduled jobs)'
& $Nssm set $ServiceName Description      'Single owner for host-side recurring jobs. Replaces the ADA/shared-infra Windows scheduled tasks, which rendered console windows on every fire.'
& $Nssm set $ServiceName Start            SERVICE_AUTO_START
& $Nssm set $ServiceName AppStdout        (Join-Path $LogDir 'service-stdout.log')
& $Nssm set $ServiceName AppStderr        (Join-Path $LogDir 'service-stderr.log')
& $Nssm set $ServiceName AppRotateFiles   1
& $Nssm set $ServiceName AppRotateBytes   10485760
# Give jobs in flight time to finish before a stop escalates to a kill.
& $Nssm set $ServiceName AppStopMethodConsole 30000
& $Nssm set $ServiceName AppExit Default Restart
& $Nssm set $ServiceName AppRestartDelay 10000

Start-Service -Name $ServiceName
Start-Sleep -Seconds 5
$svc = Get-Service -Name $ServiceName
Write-Host "service $ServiceName is $($svc.Status)"
if ($svc.Status -ne 'Running') { throw "service failed to start - check $LogDir\service-stderr.log" }

# Prove the loop is alive before disabling anything that it replaces.
$hb = Join-Path $Root 'state\heartbeat.json'
$deadline = (Get-Date).AddSeconds(90)
while (-not (Test-Path $hb) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 5 }
if (-not (Test-Path $hb)) {
    throw "no heartbeat after 90s - service is up but the loop is not ticking. Task Scheduler left untouched."
}
Write-Host "heartbeat confirmed: $(Get-Content $hb -Raw)"

# ------------------------------------------------------------------- tasks
if ($SkipTasks) {
    Write-Host ''
    Write-Host 'SkipTasks set - scheduled tasks left enabled. Both schedulers are now'
    Write-Host 'running the same jobs; re-run without -SkipTasks to retire the tasks.'
    return
}

Write-Host ''
Write-Host '== disabling migrated tasks =='
foreach ($t in $MigratedTasks) {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if (-not $task) { Write-Warning "not found: $t"; continue }
    if ($task.State -eq 'Disabled') { Write-Host "already disabled: $t"; continue }
    Disable-ScheduledTask -TaskName $t | Out-Null
    Write-Host "disabled: $t"
}

Write-Host ''
Write-Host '== retiring claude.exe-driven tasks =='
foreach ($t in $RetiredClaudeTasks) {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if (-not $task) { Write-Warning "not found: $t"; continue }
    if ($task.State -eq 'Disabled') { Write-Host "already disabled: $t"; continue }
    Disable-ScheduledTask -TaskName $t | Out-Null
    Write-Host "disabled: $t"
}

Write-Host ''
Write-Host '== remaining enabled non-Microsoft tasks =='
Get-ScheduledTask |
    Where-Object { $_.TaskPath -notlike '*Microsoft*' -and $_.State -ne 'Disabled' } |
    Select-Object TaskName, State, @{n = 'Logon'; e = { $_.Principal.LogonType } } |
    Format-Table -AutoSize

Write-Host ''
Write-Host "done. hostcron owns $jobCount jobs. Ledger: $Root\state\runs.jsonl"
Write-Host "Undo everything with: .\install-hostcron.ps1 -Revert"
