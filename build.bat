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

echo [1/3] Installing dependencies...
python -m pip install --quiet --upgrade -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    goto :fail
)

echo [2/3] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [3/3] Running PyInstaller...
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
