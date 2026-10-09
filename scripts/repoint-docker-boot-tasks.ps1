<#
Re-registers the two Docker boot tasks under SharedInfra- names that run the versioned scripts in
C:\code\shared-infra\scripts instead of the C:\tmp shims, then removes the old ZeroInfra- tasks.
Triggers, principal and settings are copied from the existing tasks unchanged. Needs an elevated
prompt because both tasks run with RunLevel Highest.
#>
$ErrorActionPreference = 'Stop'
$log = 'C:\code\shared-infra\scripts\hostcron\logs\docker-boot-tasks.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
$map = @(
    @{ Old = 'ZeroInfra-DockerAtBoot';     New = 'SharedInfra-DockerAtBoot';     Script = 'C:\code\shared-infra\scripts\docker-boot-start.ps1' },
    @{ Old = 'ZeroInfra-DockerGuiReclaim'; New = 'SharedInfra-DockerGuiReclaim'; Script = 'C:\code\shared-infra\scripts\docker-gui-reclaim.ps1' }
)
foreach ($m in $map) {
    $old = Get-ScheduledTask -TaskName $m.Old -ErrorAction SilentlyContinue
    if (-not $old) {
        if (Get-ScheduledTask -TaskName $m.New -ErrorAction SilentlyContinue) { "$($m.New) already registered"; continue }
        throw "neither $($m.Old) nor $($m.New) exists"
    }
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$($m.Script)`""
    Register-ScheduledTask -TaskName $m.New -Action $action -Trigger $old.Triggers -Principal $old.Principal `
        -Settings $old.Settings -Description "Versioned in C:\code\shared-infra (replaces $($m.Old), 2026-10-09)" -Force | Out-Null
    Unregister-ScheduledTask -TaskName $m.Old -Confirm:$false
    $line = "$(Get-Date -Format o) repoint-docker-boot-tasks: $($m.Old) -> $($m.New) running $($m.Script)"
    Add-Content -Path $log -Value $line
    $line
}
