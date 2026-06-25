@echo off
REM Datasheet MCP index build — pushes registers, prose, pins, and graph edges
REM to Qdrant. Reads QDRANT_URL / QDRANT_API_KEY from mcp/.env.
REM
REM Usage:
REM   build.bat --part ADXL345               index/refresh one part
REM   build.bat --part ADXL345 --reset       DROP all ds_* collections then rebuild
REM                                          (required when switching embedding models)
REM   build.bat --part ADXL345 --no-prose --no-graph   registers + pins only (fastest)
REM
REM Prerequisite stages (run once per part, separately):
REM   python tools/pdf_to_md.py          --part <P>   PDF -> chapter markdown
REM   python tools/extract_structured.py --part <P>   LLM -> registers/pins JSON

setlocal
cd /d "%~dp0"

echo.
echo [1/1] Building and pushing index to Qdrant...
python build_helper.py %*
if errorlevel 1 (
    echo ERROR: build failed.
    exit /b 1
)

echo.
echo Done. Start the server with:  python server.py
endlocal
