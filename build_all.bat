@echo off
REM ============================================================
REM  Datasheet MCP — Full Pipeline Orchestrator
REM ============================================================
REM
REM  Runs the complete 4-stage ingestion pipeline for one or
REM  more IC datasheet PDFs.
REM
REM  Stages:
REM    1. pdf_to_md.py         PDF -> Markdown via MinerU
REM    2. extract_structured.py Markdown -> JSON (heuristic, no LLM)
REM    3. describe_images.py   VLM figure descriptions (optional)
REM    4. build.bat            Embed + push to Qdrant
REM
REM  Usage:
REM    build_all.bat                           interactive mode
REM    build_all.bat --part ADXL345            single part
REM    build_all.bat --part ADXL345 --index-only   re-index only
REM    build_all.bat --part ADXL345 --yes      non-interactive
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Resolve Python — prefer .venv if present
set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

echo.
echo  ==============================================
echo   Datasheet MCP — Build Pipeline
echo  ==============================================
echo.
echo  Python: %PYTHON%
echo.

"%PYTHON%" "%~dp0tools\build_all.py" %*
if errorlevel 1 (
    echo.
    echo  ERROR: Pipeline failed.
    exit /b 1
)

echo.
echo  ==============================================
echo   Pipeline complete!
echo  ==============================================
endlocal
