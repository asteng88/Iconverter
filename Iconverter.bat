@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0icon_converter.py" %*
) else (
    uv run python "%~dp0icon_converter.py" %*
)
