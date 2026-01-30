
@echo off
REM Batch file wrapper for monitor.py to ensure correct working directory
REM This ensures .env and monitors.yaml are found when run from Task Scheduler

REM Get the directory where this batch file is located
set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

REM Use the Python from the virtual environment
set PYTHON_EXE=%SCRIPT_DIR%JobMonitor.venv\Scripts\python.exe

REM Check if Python executable exists
if not exist "%PYTHON_EXE%" (
    echo ERROR: Python executable not found at %PYTHON_EXE%
    echo Please check your virtual environment path.
    exit /b 1
)

REM Run the monitor script
"%PYTHON_EXE%" monitor.py

REM Capture exit code
set EXIT_CODE=%ERRORLEVEL%

REM Log the result (optional - uncomment if you want a log file)
REM echo [%date% %time%] Monitor finished with exit code %EXIT_CODE% >> "%SCRIPT_DIR%logs\batch_run.log"

exit /b %EXIT_CODE%
