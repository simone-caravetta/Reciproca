@echo off
REM Build Reciproca into a standalone Windows folder (dist\Reciproca\).
REM Run this on Windows - PyInstaller cannot cross-compile a .exe from Linux/macOS.

setlocal

echo ============================================
echo  Building Reciproca
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH.
    echo         Install Python 3 and tick "Add Python to PATH" during setup.
    goto :fail
)

REM Print the environment before doing anything. A machine with more than one
REM Python, or one whose Tk differs from the machine that built successfully,
REM produces failures that are otherwise very hard to place.
echo [1/4] Checking environment...
python -c "import sys; print('  Python     :', sys.version.split()[0]); print('  Executable :', sys.executable)"
if errorlevel 1 goto :fail

python -c "import tkinter; print('  Tk         :', tkinter.TkVersion)"
if errorlevel 1 (
    echo.
    echo [ERROR] tkinter is not available in this Python.
    echo         Re-run the Python installer, choose Modify, and tick
    echo         "tcl/tk and IDLE". It is an optional component and is easy
    echo         to leave out during a custom install.
    goto :fail
)
echo.

REM Output is deliberately not suppressed here. A silent pip step hides which
REM interpreter it installed into, which is exactly the kind of mismatch that
REM makes a build fail on one machine and work on another.
echo [2/4] Installing dependencies...
python -m pip install --upgrade -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    goto :fail
)
echo.
echo   PyInstaller version:
python -m PyInstaller --version
if errorlevel 1 (
    echo [ERROR] PyInstaller was installed but cannot be run by this Python.
    echo         You most likely have more than one Python installed and pip
    echo         installed into a different one.
    goto :fail
)
echo.

REM Not optional: PyInstaller caches its analysis in build\, so a stale cache
REM can survive a dependency upgrade and quietly undo it.
echo [3/4] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [4/4] Running PyInstaller...
python -m PyInstaller reciproca.spec --noconfirm
if errorlevel 1 (
    echo [ERROR] Build failed.
    goto :fail
)

echo.
echo ============================================
echo  Done: dist\Reciproca\Reciproca.exe
echo ============================================
echo.
echo Keep the whole dist\Reciproca folder together - the .exe needs the files
echo next to it. Copy that folder anywhere you can write to; the app stores its
echo queue, login profile and settings inside it.
echo.
pause
exit /b 0

:fail
echo.
echo Build did not complete.
pause
exit /b 1
