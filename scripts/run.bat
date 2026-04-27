@echo off
cd /d "%~dp0\.."

if "%~1"=="" (
    echo Usage:
    echo   scripts\run.bat agent ^<agent_config.json^>
    echo   scripts\run.bat pilot ^<pilot_config.json^>
    exit /b 1
)

set MODE=%~1
shift

if "%MODE%"=="agent" (
    python main.py %*
) else if "%MODE%"=="pilot" (
    python pilot.py %*
) else (
    echo Unknown mode: %MODE% (use 'agent' or 'pilot')
    exit /b 1
)
