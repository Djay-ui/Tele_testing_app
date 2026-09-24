@echo off
setlocal enabledelayedexpansion

rem ============================================================================
rem TG DataBridge - rebuild the standalone .exe
rem
rem Double-click this file. When it finishes, the package's "App" folder holds
rem the UPDATED "TG DataBridge.exe" -- same layout as
rem before, so everything else about how you use the tool is unchanged. The
rem old App folder is replaced in place -- no backup copy is kept, so close
rem the migration tool first if it is running (Windows won't let this
rem script touch a folder holding a file that is still open).
rem
rem Why a rebuild is needed at all: PyInstaller compiles the Python source
rem into the executable's own archive. There are no .py files inside App to
rem edit -- the frozen importer loads modules from inside the .exe, ahead of
rem anything on disk. So the fixed source has to be rebuilt into a new .exe.
rem
rem Requirements on THIS machine: Python 3.10 or newer, and an internet
rem connection for pip. The finished App folder needs neither -- that is the
rem whole point of the build. Roughly 5-10 minutes the first time (mostly
rem downloading Qt and the database drivers), about a minute afterwards.
rem ============================================================================

cd /d "%~dp0"
set "PKG_ROOT=%~dp0.."

echo.
echo  TG DataBridge - rebuilding the standalone executable
echo  ========================================================================
echo.

rem --- locate Python -----------------------------------------------------
set "PYTHON_CMD="

where py >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)

rem Python is only needed to BUILD. The App folder this produces runs on any
rem Windows machine with no Python at all -- so installing it here is a
rem one-off cost on the build machine, not a new dependency for users.
if not defined PYTHON_CMD (
    echo  [!] Python was not found on this machine.
    echo.

    where winget >nul 2>nul
    if not errorlevel 1 (
        echo      Windows Package Manager ^(winget^) is available, so it can be
        echo      installed automatically -- about 30 seconds, no admin rights
        echo      needed for a per-user install.
        echo.
        choice /C YN /M "      Install Python 3.13 now"
        if !errorlevel! equ 1 (
            echo.
            echo      Installing Python...
            winget install -e --id Python.Python.3.13 --scope user --accept-source-agreements --accept-package-agreements
            echo.
            rem winget updates PATH for *new* processes, so re-resolve here
            rem rather than telling the user to reopen the window.
            for /f "delims=" %%P in ('where py 2^>nul') do set "PYTHON_CMD=py -3"
            if not defined PYTHON_CMD (
                for /f "delims=" %%P in ('where python 2^>nul') do set "PYTHON_CMD=python"
            )
            if not defined PYTHON_CMD (
                if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
                    set "PYTHON_CMD="%LOCALAPPDATA%\Programs\Python\Python313\python.exe""
                )
            )
        )
    )

    if not defined PYTHON_CMD (
        echo.
        echo  [X] Cannot build without Python.
        echo.
        echo      Install Python 3.10 or newer from
        echo          https://www.python.org/downloads/
        echo      and tick "Add python.exe to PATH" in the installer, then run
        echo      this file again.
        echo.
        echo      Or run this file on any other Windows machine that already
        echo      has Python, and copy the resulting "App" folder back here --
        echo      the machine that RUNS the app needs no Python, only the one
        echo      that builds it.
        echo.
        pause
        exit /b 1
    )
)

echo  [1/6] Using Python: !PYTHON_CMD!
!PYTHON_CMD! --version
echo.

rem --- virtual environment ----------------------------------------------
if exist ".venv-build\Scripts\python.exe" (
    echo  [2/6] Reusing the existing build environment ^(.venv-build^)
) else (
    echo  [2/6] Creating the build environment ^(.venv-build^)...
    !PYTHON_CMD! -m venv .venv-build
    if errorlevel 1 (
        echo  [X] Could not create the virtual environment.
        pause
        exit /b 1
    )
)
set "VPY=.venv-build\Scripts\python.exe"
echo.

rem --- dependencies ------------------------------------------------------
echo  [3/6] Installing dependencies ^(this is the slow step^)...
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo  [X] Installing requirements.txt failed. Scroll up for the reason --
    echo      it is usually no internet connection, or a proxy blocking pip.
    pause
    exit /b 1
)
"%VPY%" -m pip install pyinstaller pytest --quiet
if errorlevel 1 (
    echo  [X] Installing PyInstaller failed.
    pause
    exit /b 1
)
echo.

rem --- tests -------------------------------------------------------------
echo  [4/6] Running the test suite before building...
"%VPY%" -m pytest tests -q --ignore=tests\integration
if errorlevel 1 (
    echo.
    echo  [!] Some tests failed. The build will continue, but look at the
    echo      failures above before shipping this executable to anyone.
    echo.
    pause
)
echo.

rem --- build -------------------------------------------------------------
echo  [5/6] Building the executable...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
"%VPY%" -m PyInstaller packaging\tg_databridge.spec --noconfirm
if errorlevel 1 (
    echo  [X] The build failed. Scroll up for the PyInstaller error.
    pause
    exit /b 1
)

if not exist "dist\App\TG DataBridge.exe" (
    echo  [X] The build reported success but produced no executable.
    pause
    exit /b 1
)
echo.

rem --- install into the package -----------------------------------------
echo  [6/6] Installing the new build into the package's App folder...

rem No backup copy is kept any more -- but the *mechanism* for getting the
rem old App folder out of the way is still an atomic move, not an in-place
rem delete. rmdir /s on a folder holding a file Windows still has open
rem deletes everything else in the tree first and only then fails on the
rem locked file, leaving App itself half-destroyed under the running
rem program's feet. move/rename either succeeds completely or fails
rem completely -- so a "still running" failure here leaves App exactly as
rem it was, safe to just close the tool and try again, exactly as before.
rem The moved-aside copy is deleted for real only after the new build is
rem confirmed in place below, so nothing durable is ever kept.
if exist "%PKG_ROOT%\App-backup-OLD" (
    echo        Removing the old "App-backup-OLD" folder from a previous build...
    rmdir /s /q "%PKG_ROOT%\App-backup-OLD"
)

if exist "%PKG_ROOT%\App" (
    echo        Moving the current App folder aside...
    move /y "%PKG_ROOT%\App" "%PKG_ROOT%\App-backup-OLD" >nul
    if errorlevel 1 (
        echo  [X] Could not move the existing App folder aside. It is probably
        echo      still running -- close the migration tool and try again.
        pause
        exit /b 1
    )
)

echo        Copying the new build into place...
rem robocopy uses exit codes 0-7 for success; 8 and above are real failures.
robocopy "dist\App" "%PKG_ROOT%\App" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo  [X] Copying the new build failed. The old build is still intact at:
    echo      %PKG_ROOT%\App-backup-OLD
    echo      The build output is also available at:
    echo      %CD%\dist\App
    pause
    exit /b 1
)

if not exist "%PKG_ROOT%\App\TG DataBridge.exe" (
    echo  [X] The new App folder is missing the executable. The old build is
    echo      still intact at %PKG_ROOT%\App-backup-OLD -- rename it back to
    echo      "App" to keep using it while this gets sorted out.
    pause
    exit /b 1
)

if exist "%PKG_ROOT%\App-backup-OLD" (
    echo        Removing the old build ^(no backup is kept^)...
    rmdir /s /q "%PKG_ROOT%\App-backup-OLD"
)

echo.
echo  ========================================================================
echo   Done. The package is updated in place.
echo.
echo   Run it the same way you always have:
echo       App\TG DataBridge.exe
echo.
echo   No backup of the previous build was kept -- only "App" exists now.
echo.
echo   Note: a SQL Server *target* additionally needs a Microsoft ODBC Driver
echo   17 or 18 installed on whichever machine runs the app. That is an
echo   OS-level driver and cannot be bundled into the .exe.
echo  ========================================================================
echo.
pause
