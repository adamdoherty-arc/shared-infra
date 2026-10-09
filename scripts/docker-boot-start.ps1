# Run by the ZeroInfra-DockerAtBoot scheduled task (AtStartup +4 min, S4U as hadam, session 0) through the
# C:\tmp\start-docker-if-down.ps1 shim. The task name and shim path predate Zero's retirement; repointing the
# task needs elevation, so the shim forwards here and this file is the versioned source.
#
# Starts Docker Desktop when the engine is down after a boot. 2026-10-09 incident: after an unclean reboot this
# launched Docker Desktop in session 0 at 12:31Z, the operator logged on at ~12:33Z, and the logon reclaim task
# then ran `docker desktop stop` to move the GUI into session 1. That quit marked every container manually
# stopped, and ada-bitcoin-prod, ada-nginx, ERPNext, customer-ops and fortressos stayed Exited for 8 minutes.
# So when an interactive session exists (or appears within $WaitForLogonS) this script leaves the launch to
# the logon side (HKCU Run + docker-gui-reclaim.ps1), which starts Docker Desktop directly in session 1 and
# never has to restart it. Only a box nobody logs on to gets the session-0 launch.
param([int]$WaitForLogonS = 240)
$ErrorActionPreference = 'SilentlyContinue'
$log = 'C:\code\shared-infra\scripts\hostcron\logs\docker-boot-tasks.log'
function Log([string]$m) { Add-Content -Path $log -Value ("{0} boot-start {1}" -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $m) }
function Engine-Up {
  $null = & docker version --format '{{.Server.Version}}' 2>$null
  return $LASTEXITCODE -eq 0
}
function Interactive-Session {
  return [bool](Get-Process -Name explorer -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -ne 0 })
}
function Gui-Running { return [bool](Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue) }

if (Engine-Up) { Log 'engine already up - nothing to do'; exit 0 }
for ($i = 0; $i -lt [math]::Ceiling($WaitForLogonS / 10); $i++) {
  if (Engine-Up) { Log 'engine came up while waiting - nothing to do'; exit 0 }
  if (Interactive-Session) { Log 'interactive session present - leaving the launch to the logon reclaim task (session 1)'; exit 0 }
  if (Gui-Running) { Log 'Docker Desktop already launching - nothing to do'; exit 0 }
  Start-Sleep -Seconds 10
}
Log "no interactive logon within ${WaitForLogonS}s - launching Docker Desktop in session 0"
Start-Service com.docker.service -ErrorAction SilentlyContinue
Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe' -ArgumentList '-Autostart'
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 10
  if (Engine-Up) { Log "engine up after $(($i+1)*10)s"; exit 0 }
}
Log 'engine did NOT come up within 300s'
exit 1
