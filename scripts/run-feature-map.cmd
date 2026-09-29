@echo off
REM Wrapper for the hostcron job "ada-feature-map-nightly" (daily 05:10).
REM Rebuilds ADA's derived per-feature architecture maps (docs/architecture/**, generated nested CLAUDE.md)
REM from Legion + git + the codegraph-free AST scan (Enhancement-1001108). Zero LLM tokens.
REM No commit here: ADA is a multi-agent trunk whose commit hooks need a Legion task, so the rebuilt files
REM stay on disk and the feature_map_fresh pre-commit gate makes the next commit that touches a feature carry its map.
setlocal
set "ADA=C:\code\ADA"
set "LOG=C:\code\shared-infra\state\claude-usage\feature-map.log"
if not exist "C:\code\shared-infra\state\claude-usage" mkdir "C:\code\shared-infra\state\claude-usage"
cd /d "%ADA%"
echo [%date% %time%] === feature map start === >> "%LOG%"
python "%ADA%\scripts\architecture\build_feature_map.py" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] === feature map done rc=%RC% === >> "%LOG%"
endlocal & exit /b %RC%
