@echo off
setlocal enabledelayedexpansion

rem ============================================================================
rem TG DataBridge - Windows launcher
rem Double-click this file to start the app (equivalent to running
rem "python main.py" from a cmd prompt in this folder).
rem ============================================================================

cd /d "%~dp0"

rem If nothing below finds Python automatically, uncomment the next line and
rem set it to the full path of your python.exe. Find that path by opening a
rem Command Prompt where "python main.py" already works and running:
rem     where python
rem set "PYTHON_EXE_OVERRIDE=C:\path\to\python.exe"

set "PYTHON_CMD="

if defined PYTHON_EXE_OVERRIDE (
    if exist "%PYTHON_EXE_OVERRIDE%" set "PYTHON_CMD="%PYTHON_EXE_OVERRIDE%""
)

if not defined PYTHON_CMD (
    if exist ".venv\Scripts\python.exe" set "PYTHON_CMD=".venv\Scripts\python.exe""
)

rem Try the Windows "py" launcher first -- it's registered by the official
rem python.org installer regardless of whether "Add python.exe to PATH" was
rem checked, so it's the most reliable way to find Python on Windows.
if not defined PYTHON_CMD (
    where py >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    where python3 >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python3"
)

rem Last resort: check the common per-user and per-machine install locations
rem directly, in case none of "py"/"python"/"python3" are on PATH for
rem double-clicked files even though they work in an already-open cmd window
rem (this happens right after installing/adding to PATH, before the next
rem sign-in or Explorer restart picks up the change).
if not defined PYTHON_CMD (
    for %%D in (
        "%LocalAppData%\Programs\Python\Python313\python.exe"
        "%LocalAppData%\Programs\Python\Python312\python.exe"
        "%LocalAppData%\Programs\Python\Python311\python.exe"
        "%LocalAppData%\Programs\Python\Python310\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
    ) do (
        if not defined PYTHON_CMD if exist %%D set "PYTHON_CMD="%%~D""
    )
)

if not defined PYTHON_CMD (
    echo.
    echo ERROR: Could not find Python on this machine via "py", "python", or "python3".
    echo.
    echo If "python main.py" already works when typed directly into an open
    echo Command Prompt window, Python IS installed -- this launcher just isn't
    echo finding it in the PATH that double-clicked files get. This is common
    echo right after installing Python or editing PATH, before the next sign-out/
    echo sign-in or Explorer restart.
    echo.
    echo Quick fixes, in order of ease:
    echo   1. Sign out and back in ^(or restart your PC^), then try this file again.
    echo   2. Open Command Prompt, run:  where python
    echo      then edit this .bat file: uncomment PYTHON_EXE_OVERRIDE near the top
    echo      and paste that exact path in.
    echo   3. Reinstall Python from https://www.python.org/downloads/ and check
    echo      "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

echo Starting TG DataBridge using: %PYTHON_CMD%
%PYTHON_CMD% main.py
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo The application exited with an error ^(code %EXIT_CODE%^) -- see above.
    echo If this is the first time running it, install the required packages first:
    echo   %PYTHON_CMD% -m pip install -r requirements.txt
    echo.
    pause
)

endlocal
