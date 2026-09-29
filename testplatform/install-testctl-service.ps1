#requires -Version 5.1
<#
.SYNOPSIS
  Optional NSSM install of the testctl runner service (run ELEVATED). The default supervision is the
  hostcron job "testctl-serve-ensure"; use this only if you want a dedicated Windows service instead.
#>
param([switch]$Revert, [string]$Python = 'C:\Python314\python.exe', [string]$Nssm = 'C:\ProgramData\chocolatey\bin\nssm.exe')
$ErrorActionPreference = 'Stop'
$Name = 'ADA-TestctlServe'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root 'logs'
if ($Revert) { & $Nssm stop $Name confirm 2>&1 | Out-Null; & $Nssm remove $Name confirm 2>&1 | Out-Null; return }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
& $Nssm install $Name $Python (Join-Path $Root 'testctl.py') serve
& $Nssm set $Name AppDirectory $Root
& $Nssm set $Name AppEnvironmentExtra 'PYTHONPATH=C:\Users\hadam\AppData\Roaming\Python\Python314\site-packages'
& $Nssm set $Name Start SERVICE_AUTO_START
& $Nssm set $Name AppStdout (Join-Path $LogDir 'service-stdout.log')
& $Nssm set $Name AppStderr (Join-Path $LogDir 'service-stderr.log')
& $Nssm set $Name AppRotateFiles 1
& $Nssm set $Name AppRotateBytes 10485760
& $Nssm set $Name AppExit Default Restart
Start-Service -Name $Name
