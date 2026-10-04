@echo off
REM Wrapper for hostcron job "ada-daily-plan-commit" (daily 06:40, after ADA's daily_plan_render at 06:20 ET):
REM lands the generated docs/plans/daily files and a fresh STRUCTURE.md through ADA's safe_commit.sh. Zero LLM tokens.
setlocal
set "LOG=C:\code\shared-infra\state\claude-usage\daily-plan-commit.log"
if not exist "C:\code\shared-infra\state\claude-usage" mkdir "C:\code\shared-infra\state\claude-usage"
cd /d C:\code\ADA
echo [%date% %time%] === daily plan commit start === >> "%LOG%"
"C:\Program Files\Git\bin\bash.exe" "C:/code/shared-infra/scripts/daily-plan-commit.sh" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] === daily plan commit done rc=%RC% === >> "%LOG%"
endlocal & exit /b %RC%
