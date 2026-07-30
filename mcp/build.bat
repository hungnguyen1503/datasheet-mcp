@echo off
REM Datasheet MCP index build — pushes registers, prose, pins, and graph edges
REM to Qdrant. Reads QDRANT_URL / QDRANT_API_KEY from mcp/.env.
REM
REM Usage:
REM   build.bat --part ADXL345               index/refresh one part
REM   build.bat --part ADXL345 --reset       DROP all ds_* collections then rebuild
REM   Prose and graph evidence are mandatory in the canonical index.

setlocal
cd /d "%~dp0"

REM Resolve Python — prefer repo .venv
set "PYTHON=python"
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYTHON=%~dp0..\.venv\Scripts\python.exe"

echo.
echo [1/1] Building and pushing index to Qdrant...
"%PYTHON%" "%~dp0build_helper.py" %*
if errorlevel 1 (
    echo ERROR: build failed.
    exit /b 1
)

echo.
echo Done. Start the server with:  python server.py
endlocal
