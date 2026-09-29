@echo off
REM Wrapper for the hostcron job "ada-feature-map-nightly" (daily 05:10).
REM Rebuilds ADA's derived per-feature architecture maps (docs/architecture/**, generated nested CLAUDE.md)
REM from Legion + git + the codegraph-free AST scan (Enhancement-1001108), then commits only the changed
REM generated files (--sync-legion also adds derived ownership to Legion code_paths, Enhancement-1001109) through ADA's safe_commit.sh (feature-map-commit.sh). Zero LLM tokens.
setlocal
set "ADA=C:\code\ADA"
set "LOG=C:\code\shared-infra\state\claude-usage\feature-map.log"
if not exist "C:\code\shared-infra\state\claude-usage" mkdir "C:\code\shared-infra\state\claude-usage"
cd /d "%ADA%"
echo [%date% %time%] === feature map start === >> "%LOG%"
python "%ADA%\scripts\architecture\build_feature_map.py" --sync-legion >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  "C:\Program Files\Git\bin\bash.exe" "C:/code/shared-infra/scripts/feature-map-commit.sh" >> "%LOG%" 2>&1
  set "RC=%ERRORLEVEL%"
)
echo [%date% %time%] === feature map done rc=%RC% === >> "%LOG%"
endlocal & exit /b %RC%
