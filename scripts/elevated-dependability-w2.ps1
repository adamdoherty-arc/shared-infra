<#
.SYNOPSIS
    The ONLY steps of Legion sprint 15209 (Dependability W2) that need an elevated shell.

.DESCRIPTION
    Run once from an Administrator PowerShell:
        powershell -ExecutionPolicy Bypass -File C:\code\shared-infra\scripts\elevated-dependability-w2.ps1
    Add -RestartPostgres to also apply listen_addresses='localhost' (already written to
    postgresql.conf; takes effect only on a restart, ~10 s PG outage, ADA reconnects on its own).
    Add -CompactDocker to also reclaim the ~290 GB of VHDX slack (stops Docker for ~20-40 min;
    scripts\compact-docker-disk.ps1 brings every stack back and hostcron ops-reconcile heals the rest).

    Default (no switches) does only the two harmless steps:
      1. Disables the two Task Scheduler tasks that duplicate hostcron jobs and could not be disabled
         without elevation (double execution + log collisions, INF-08):
           - "ADA LLM Weekly Report"            (hostcron: ada-llm-weekly-report)
           - "Shared Infra - WSL Memory Reclaim" (hostcron: wsl-memory-reclaim)
      2. Adds a Windows Firewall inbound BLOCK for TCP 5432 on non-loopback addresses
         (defence in depth behind pg_hba.conf, which already rejects every non-loopback client).
#>
[CmdletBinding()]
param([switch]$RestartPostgres, [switch]$CompactDocker)

$ErrorActionPreference = 'Stop'
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw 'Run this from an elevated (Administrator) PowerShell.'
}

foreach ($t in 'ADA LLM Weekly Report', 'Shared Infra - WSL Memory Reclaim') {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Disable-ScheduledTask -TaskName $t | Out-Null
        Write-Host "disabled task: $t"
    }
}

$rule = 'ADA-PG-5432-block-nonloopback'
if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Block -Protocol TCP -LocalPort 5432 `
        -RemoteAddress @('192.168.0.0/16', '10.0.0.0/8', '100.64.0.0/10', '169.254.0.0/16') | Out-Null
    Write-Host "firewall: blocked inbound 5432 from LAN/Tailscale ranges ($rule)"
}

if ($RestartPostgres) {
    Write-Host 'restarting postgresql-x64-17 (applies listen_addresses=localhost)'
    Restart-Service -Name 'postgresql-x64-17' -Force
    Start-Sleep 8
    Get-Service 'postgresql-x64-17' | Format-Table Name, Status -AutoSize
}

if ($CompactDocker) {
    & 'C:\code\shared-infra\scripts\compact-docker-disk.ps1'
}
