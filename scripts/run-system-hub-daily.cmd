@echo off
REM System Hub daily review ring (Fix-1100000738). hostcron job system-hub-daily, 06:30 ET.
REM Headless claude.exe under explicit caps; hostcron runs as LocalSystem, so the
REM interactive user's profile is pinned for the Claude Code login.
setlocal
set "USERPROFILE=C:\Users\hadam"
set "HOME=C:\Users\hadam"
set "APPDATA=C:\Users\hadam\AppData\Roaming"
set "LOCALAPPDATA=C:\Users\hadam\AppData\Local"
set "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0"
set "REPO=C:\code\ADA"
set "LOG=%REPO%\.claude\logs\system-hub-daily.log"
if not exist "%REPO%\.claude\logs" mkdir "%REPO%\.claude\logs"
cd /d "%REPO%"
echo [%date% %time%] === system-hub-daily start === >> "%LOG%"
"%USERPROFILE%\.local\bin\claude.exe" -p --model opus --effort high --max-turns 150 --max-budget-usd 15 --dangerously-skip-permissions < "%REPO%\scripts\setup\system_hub_daily_prompt.md" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] === system-hub-daily exited errorlevel %RC% === >> "%LOG%"
endlocal & exit /b %RC%
