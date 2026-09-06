@echo off
REM Run csprpc without installing anything.
REM   csprpc.cmd doctor
REM   csprpc.cmd run
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -m csprpc %*
) else (
    python -m csprpc %*
)
endlocal
