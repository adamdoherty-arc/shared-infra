@echo off
REM Wrapper invoked by the Windows task "Claude Usage Forensics - Weekly" (Sun 08:00).
REM Pure Python, zero LLM tokens: walks ~/.claude/projects/**/*.jsonl and writes
REM   docs\claude-usage\hosts\<host>\README.md     this machine's page (rendered Markdown)
REM   docs\claude-usage\hosts\<host>\latest.json   the raw report
REM   docs\claude-usage\hosts\<host>\history\<date>.json one file per run, feeds the trend table
REM   docs\claude-usage\README.md                 cross-host index (scripts\claude_usage_index.py)
REM Transcripts are per machine, so every machine runs its own copy of this task.
REM then commits exactly those paths so the page is versioned. state\ is gitignored,
REM so the run log lives there. No push: publishing stays a deliberate `git push`.
setlocal

set "REPO=C:\code\shared-infra"
set "OUT=%REPO%\docs\claude-usage\hosts\%COMPUTERNAME%"
if not exist "%OUT%\history" mkdir "%OUT%\history"
set "LOG=%REPO%\state\claude-usage\usage-forensics.log"
if not exist "%REPO%\state\claude-usage" mkdir "%REPO%\state\claude-usage"

cd /d "%REPO%"
echo [%date% %time%] === claude usage forensics start === >> "%LOG%"
python "%REPO%\scripts\claude_usage_forensics.py" --days 7 --json "%OUT%\latest.json" --history-dir "%OUT%\history" --markdown "%OUT%\README.md" --discord >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] forensics exited errorlevel %RC% >> "%LOG%"
if not "%RC%"=="0" goto :done
python "%REPO%\scripts\claude_usage_index.py" >> "%LOG%" 2>&1

REM Path-scoped commit only -- never `git add -A`; other sessions keep dirty files here.
git add -- docs/claude-usage >> "%LOG%" 2>&1
git diff --cached --quiet -- docs/claude-usage
if "%ERRORLEVEL%"=="0" (
  echo [%date% %time%] nothing new to commit >> "%LOG%"
  goto :done
)
git commit -m "claude-usage: weekly forensics %date%" -- docs/claude-usage >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] commit exited errorlevel %RC% ^(files are on disk regardless^) >> "%LOG%"

:done
echo [%date% %time%] === claude usage forensics done rc=%RC% === >> "%LOG%"
endlocal & exit /b %RC%
