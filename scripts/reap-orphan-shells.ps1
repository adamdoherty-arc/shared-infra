<#
.SYNOPSIS
  Kill shell trees whose owning Claude Code session has exited.

.DESCRIPTION
  Closing a Claude Code session does not take its Bash-tool children with it.
  The bash.exe processes are reparented and keep running - along with whatever
  they were driving. Measured 2026-09-23 after closing 14 sessions: 21 orphaned
  bash roots survived, one still driving a full vitest suite through cmd + six
  node processes, another looping `docker logs ada-scheduler` every ~15s. Each
  child spawns a console window in the interactive session, so the flicker
  outlives the session that caused it, indefinitely.

  Reaping the 22 orphan trees took session-1 console spawns from 55/min to
  6/min, and bash/sh processes from 59 to 11.

  An orphan is identified as a bash.exe/sh.exe in session 1 whose parent PID no
  longer maps to a live process. Live sessions are never touched: their shells
  still have a reachable claude.exe ancestor.

.PARAMETER WhatIf
  List what would be killed and exit.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\reap-orphan-shells.ps1 -WhatIf
  powershell -ExecutionPolicy Bypass -File .\reap-orphan-shells.ps1
#>
[CmdletBinding()]
param([switch]$WhatIf)
$ErrorActionPreference = 'Stop'

$all = @{}
foreach ($q in (Get-CimInstance Win32_Process)) { $all[[int]$q.ProcessId] = $q }

$orphans = @()
foreach ($q in $all.Values) {
    if ($q.Name -notmatch '^(bash|sh)\.exe$') { continue }
    $sid = (Get-Process -Id $q.ProcessId -ErrorAction SilentlyContinue).SessionId
    # session 1 only: session 0 shells belong to services and have no window
    if ($sid -ne 1) { continue }
    if ($all.ContainsKey([int]$q.ParentProcessId)) { continue }
    $orphans += $q
}

if (-not $orphans) { Write-Host 'no orphaned shells'; return }

$beforeConhost = @(Get-Process conhost -ErrorAction SilentlyContinue).Count
$beforeShells = @(Get-CimInstance Win32_Process -Filter "Name='bash.exe' or Name='sh.exe'").Count

foreach ($o in $orphans) {
    $cl = $o.CommandLine
    if ($cl -and $cl.Length -gt 70) { $cl = $cl.Substring(0, 70) }
    if ($WhatIf) {
        Write-Host ("  would reap pid {0} (dead parent {1})  {2}" -f $o.ProcessId, $o.ParentProcessId, $cl)
    } else {
        # /T so the whole tree goes, not just the shell - the node/cmd children
        # are what actually spawn the console windows
        & taskkill /PID $o.ProcessId /T /F 2>&1 | Out-Null
        Write-Host ("  reaped pid {0} (dead parent {1})" -f $o.ProcessId, $o.ParentProcessId)
    }
}

if ($WhatIf) { Write-Host ''; Write-Host ("WhatIf - {0} orphan tree(s) would be reaped." -f $orphans.Count); return }

Start-Sleep -Seconds 5
Write-Host ''
Write-Host ("reaped {0} orphan tree(s)" -f $orphans.Count)
Write-Host ("conhost  {0} -> {1}" -f $beforeConhost, @(Get-Process conhost -ErrorAction SilentlyContinue).Count)
Write-Host ("bash/sh  {0} -> {1}" -f $beforeShells, @(Get-CimInstance Win32_Process -Filter "Name='bash.exe' or Name='sh.exe'").Count)
