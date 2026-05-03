@echo off
REM Babson Library Scheduler — Windows launcher
REM Double-click this file to start the server.

echo.
echo =========================================
echo   Babson Library Scheduler
echo =========================================
echo.

REM Move into the babson-scheduler folder (relative to this .bat file)
cd /d "%~dp0babson-scheduler"

REM Check that .venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: Virtual environment not found.
    echo.
    echo Run this once to set it up:
    echo   cd %~dp0babson-scheduler
    echo   py -3.11 -m venv .venv
    echo   .venv\Scripts\activate
    echo   python -m pip install --upgrade pip
    echo   python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM Activate virtual environment
call .venv\Scripts\activate

echo Starting server...
echo.
echo   Admin UI  -^>  http://localhost:8000/api/v1/admin/ui
echo   API Docs  -^>  http://localhost:8000/docs
echo.
echo Press Ctrl+C to stop the server.
echo.

python -m uvicorn app.main:app --reload --port 8000

pause
