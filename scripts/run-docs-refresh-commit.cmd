@echo off
REM Wrapper for hostcron job "ada-docs-refresh-commit": commits the topic-file drift docs_refresh.py leaves on disk
REM (only files whose diff is confined below the auto marker). Zero LLM tokens.
setlocal
set "LOG=C:\code\shared-infra\state\claude-usage\docs-refresh-commit.log"
if not exist "C:\code\shared-infra\state\claude-usage" mkdir "C:\code\shared-infra\state\claude-usage"
cd /d C:\code\ADA
echo [%date% %time%] === docs refresh commit start === >> "%LOG%"
"C:\Program Files\Git\bin\bash.exe" "C:/code/shared-infra/scripts/docs-refresh-commit.sh" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] === docs refresh commit done rc=%RC% === >> "%LOG%"
endlocal & exit /b %RC%
