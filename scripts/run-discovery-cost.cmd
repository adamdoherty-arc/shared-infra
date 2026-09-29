@echo off
REM Wrapper for the hostcron job "claude-discovery-cost-weekly" (Sun 08:30).
REM Pure Python, zero LLM tokens: reads ~/.claude/projects/c--code-ADA/*.jsonl and measures how many
REM tool calls each main-thread ADA session spends BEFORE its first real edit (Enhancement-1001108).
REM Writes C:\code\ADA\.claude\state\discovery-cost\latest.json and
REM C:\code\ADA\docs\audits\discovery-cost\<date>.md. No commit here: ADA is a multi-agent trunk whose
REM commit hooks need a Legion task, so the dated page is left on disk for the next session's path-scoped commit.
setlocal
set "ADA=C:\code\ADA"
set "LOG=C:\code\shared-infra\state\claude-usage\discovery-cost.log"
if not exist "C:\code\shared-infra\state\claude-usage" mkdir "C:\code\shared-infra\state\claude-usage"
cd /d "%ADA%"
echo [%date% %time%] === discovery cost start === >> "%LOG%"
python "%ADA%\scripts\audits\discovery_cost.py" --days 7 --limit 12 >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] === discovery cost done rc=%RC% === >> "%LOG%"
endlocal & exit /b %RC%
