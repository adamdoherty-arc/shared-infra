<#
.SYNOPSIS
    The ONLY steps of the DR drill fix (Legion sprint 15431, task 21162) that need an elevated shell.

.DESCRIPTION
    Run once from an Administrator PowerShell:
        powershell -ExecutionPolicy Bypass -File C:\code\shared-infra\scripts\elevated-dr-finish.ps1
    Idempotent: a second run reports "already done" for every step and changes nothing.

      1. Restarts the ADA-HostCron service so the running daemon loads the graceful-stop ladder
         (hostcron.py + console_break.py). Only when no hostcron job is mid-run: it reads
         hostcron.log (start with no later end, bounded by each job's timeout + 1200 s) and waits up to
         -WaitMinutes (default 10) for a quiet moment; if none comes it does NOT restart and says so.
         Add -SkipRestart to skip this step.
      2. Runs install-hostcron.ps1, which unregisters the retired 'ADA DR Restore Drill - Weekly' task.
         Skipped when that task is already gone.
      3. Disables 'ADA LLM Weekly Report' and 'Shared Infra - WSL Memory Reclaim', but only after
         proving each is duplicated: an enabled hostcron job in schedule.json must run the same script
         as the task's action. A task that is not duplicated is left alone and reported.
#>
[CmdletBinding()]
param([switch]$SkipRestart, [int]$WaitMinutes = 10)

$ErrorActionPreference = 'Stop'
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw 'Run this from an elevated (Administrator) PowerShell.'
}

$hostcronDir = 'C:\code\shared-infra\scripts\hostcron'
$schedulePath = Join-Path $hostcronDir 'schedule.json'
$logPath = Join-Path $hostcronDir 'logs\hostcron.log'
$contractPath = 'C:\code\ADA\scripts\dr_drill_contract.py'
$python = 'C:\Python314\python.exe'
$service = 'ADA-HostCron'
$schedule = Get-Content -Raw -Encoding UTF8 $schedulePath | ConvertFrom-Json

# --- 1. restart the service only when no job is mid-run -----------------------------------------------
function Get-InflightJobs {
    $code = @'
import importlib.util, json, sys, time
spec = importlib.util.spec_from_file_location("c", sys.argv[1]); c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
names = [j["name"] for j in json.load(open(sys.argv[2], encoding="utf8"))["jobs"]]
lines = open(sys.argv[3], encoding="utf8", errors="replace").read().splitlines()[-20000:]
print(json.dumps([j.get("job", j.get("name")) for j in c.inflight_jobs(lines, time.time(), names, None)]))
'@
    $out = & $python -c $code $contractPath $schedulePath $logPath
    if ($LASTEXITCODE -ne 0) { throw 'could not read hostcron.log to decide whether a job is mid-run' }
    return @($out | ConvertFrom-Json)
}

Write-Host '== ADA-HostCron service =='
if ($SkipRestart) {
    Write-Host 'skipped (-SkipRestart)'
} elseif (-not (Get-Service -Name $service -ErrorAction SilentlyContinue)) {
    Write-Host "service $service is not installed; step 2 installs it"
} else {
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    do {
        $busy = Get-InflightJobs
        if ($busy.Count -eq 0) { break }
        Write-Host ("jobs mid-run: " + ($busy -join ', ') + "; waiting")
        Start-Sleep -Seconds 15
    } while ((Get-Date) -lt $deadline)
    if ($busy.Count -gt 0) {
        Write-Host ("NOT restarted: still mid-run after $WaitMinutes min: " + ($busy -join ', ') + ". Re-run when idle.")
    } else {
        $started = (Get-Process -Id (Get-CimInstance Win32_Service -Filter "Name='$service'").ProcessId -ErrorAction SilentlyContinue).StartTime
        $ladder = (Get-Item (Join-Path $hostcronDir 'hostcron.py')).LastWriteTime
        if ($started -and $started -gt $ladder) {
            Write-Host "already done: service started $started, after hostcron.py changed $ladder"
        } else {
            Restart-Service -Name $service -Force
            Start-Sleep -Seconds 5
            Write-Host ("restarted: " + (Get-Service $service).Status)
        }
    }
}

# --- 2. unregister the retired DR task through the installer --------------------------------------------
Write-Host '== retired DR drill task =='
if (Get-ScheduledTask -TaskName 'ADA DR Restore Drill - Weekly' -ErrorAction SilentlyContinue) {
    & (Join-Path $hostcronDir 'install-hostcron.ps1')
    if (Get-ScheduledTask -TaskName 'ADA DR Restore Drill - Weekly' -ErrorAction SilentlyContinue) {
        throw "install-hostcron.ps1 ran but 'ADA DR Restore Drill - Weekly' is still registered"
    }
    Write-Host 'installer ran; DR task unregistered'
} else {
    Write-Host "already done: 'ADA DR Restore Drill - Weekly' is not registered"
}

# --- 3. disable the verified duplicate tasks ---------------------------------------------------------------
Write-Host '== duplicate scheduled tasks =='
function Get-ScriptTokens($parts) {
    $tokens = @()
    foreach ($p in $parts) {
        $t = ([string]$p).Trim('"').Replace('/', '\').ToLower()
        if ($t -match '\.(py|cmd|bat|ps1)$' -and $t.Contains('\')) { $tokens += $t }
    }
    return $tokens
}
$twins = @{ 'ADA LLM Weekly Report' = 'ada-llm-weekly-report'; 'Shared Infra - WSL Memory Reclaim' = 'wsl-memory-reclaim' }
foreach ($name in $twins.Keys) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "already done: task '$name' not registered"; continue }
    if ($task.State -eq 'Disabled') { Write-Host "already done: '$name' is disabled"; continue }
    $job = $schedule.jobs | Where-Object { $_.name -eq $twins[$name] -and $_.enabled -ne $false } | Select-Object -First 1
    if (-not $job) { Write-Host "SKIPPED '$name': hostcron job '$($twins[$name])' is missing or disabled, so it is not a duplicate"; continue }
    $jobTokens = Get-ScriptTokens $job.cmd
    $taskTokens = @()
    foreach ($a in $task.Actions) { $taskTokens += Get-ScriptTokens (@($a.Execute) + ([regex]::Matches([string]$a.Arguments, '"[^"]+"|\S+') | ForEach-Object { $_.Value })) }
    $shared = @($taskTokens | Where-Object { $jobTokens -contains $_ })
    if ($shared.Count -eq 0) {
        Write-Host "SKIPPED '$name': its action ($($taskTokens -join ', ')) does not run the same script as job '$($job.name)' ($($jobTokens -join ', '))"
        continue
    }
    Disable-ScheduledTask -TaskName $name | Out-Null
    Write-Host "disabled task '$name' (duplicate of hostcron job '$($job.name)', shared script $($shared[0]))"
}
Write-Host 'done.'
