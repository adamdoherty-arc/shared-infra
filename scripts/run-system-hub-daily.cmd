@echo off
REM System Hub daily review ring (Fix-1100000738, reworked Fix-1100000753). hostcron job system-hub-daily, 06:30 ET.
REM Headless claude.exe under explicit caps; hostcron runs as LocalSystem, so the
REM interactive user's profile is pinned for the Claude Code login.
REM Audit trail: stream-json transcript in system-hub-daily.jsonl (overwritten per run),
REM start/exit/result lines in system-hub-daily.log, one line per item in system-hub-daily.md (written by the prompt).
setlocal
set "USERPROFILE=C:\Users\hadam"
set "HOME=C:\Users\hadam"
set "APPDATA=C:\Users\hadam\AppData\Roaming"
set "LOCALAPPDATA=C:\Users\hadam\AppData\Local"
set "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0"
set "REPO=C:\code\ADA"
if not defined SH_MODEL set "SH_MODEL=opus"
if not defined SH_TURNS set "SH_TURNS=150"
if not defined SH_BUDGET set "SH_BUDGET=20"
if not defined SH_PROMPT set "SH_PROMPT=%REPO%\scripts\setup\system_hub_daily_prompt.md"
set "LOG=%REPO%\.claude\logs\system-hub-daily.log"
set "STREAM=%REPO%\.claude\logs\system-hub-daily.jsonl"
if not exist "%REPO%\.claude\logs" mkdir "%REPO%\.claude\logs"
cd /d "%REPO%"
set "SNAP=%REPO%\.claude\state\system-hub-daily-snapshot.json"
C:\Python314\python.exe "%REPO%\scripts\hub_ring_leftovers.py" snapshot "%SNAP%" >> "%LOG%" 2>&1
echo [%date% %time%] === system-hub-daily start model=%SH_MODEL% turns=%SH_TURNS% budget=%SH_BUDGET% === >> "%LOG%"
"%USERPROFILE%\.local\bin\claude.exe" -p --model %SH_MODEL% --effort high --max-turns %SH_TURNS% --max-budget-usd %SH_BUDGET% --output-format stream-json --verbose --dangerously-skip-permissions < "%SH_PROMPT%" > "%STREAM%" 2>> "%LOG%"
set "RC=%ERRORLEVEL%"
findstr /c:"\"type\":\"result\"" "%STREAM%" >> "%LOG%"
if not "%RC%"=="0" C:\Python314\python.exe "%REPO%\scripts\audits\system_hub_score.py" --record >> "%LOG%" 2>&1
C:\Python314\python.exe "%REPO%\scripts\hub_ring_leftovers.py" report "%SNAP%" >> "%LOG%" 2>&1
echo [%date% %time%] === system-hub-daily exited errorlevel %RC% === >> "%LOG%"
endlocal & exit /b %RC%
