# Run from an ELEVATED PowerShell (nssm service parameters live under HKLM).
# Moves SharedInfra-Stack's nssm stdout/stderr out of the repo root into
# .runtime\nssm\ (gitignored). Takes effect at the next service restart; the
# stack keeps running -- do not restart it just for this.
$nssm = 'C:\ProgramData\chocolatey\lib\NSSM\tools\nssm.exe'
New-Item -ItemType Directory -Force 'C:\code\shared-infra\.runtime\nssm' | Out-Null
& $nssm set SharedInfra-Stack AppStdout 'C:\code\shared-infra\.runtime\nssm\stdout.log'
& $nssm set SharedInfra-Stack AppStderr 'C:\code\shared-infra\.runtime\nssm\stderr.log'
& $nssm set SharedInfra-Stack AppRotateFiles 1
& $nssm set SharedInfra-Stack AppRotateBytes 10485760
& $nssm get SharedInfra-Stack AppStdout
