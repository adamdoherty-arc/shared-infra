@echo off
REM Install/refresh the weekly Windows task "Claude Usage Forensics - Weekly" (Sunday 08:00) for THIS machine.
REM Idempotent (schtasks /F). Then runs it once so docs\claude-usage\hosts\%COMPUTERNAME%\ exists.
set "REPO=%~dp0.."
for %%I in ("%REPO%") do set "REPO=%%~fI"
schtasks /Create /F /TN "Claude Usage Forensics - Weekly" /SC WEEKLY /D SUN /ST 08:00 /TR "cmd.exe /c \"%REPO%\scripts\run-usage-forensics.cmd\"" || exit /b 1
schtasks /Query /TN "Claude Usage Forensics - Weekly" /FO LIST | findstr /C:"Next Run Time" /C:"Task To Run"
call "%REPO%\scripts\run-usage-forensics.cmd"
echo done rc=%ERRORLEVEL% -- now: cd /d "%REPO%" ^&^& git push origin master
