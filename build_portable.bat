@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   Cangai Portable Build (no 3D mode at runtime)
echo   Output: dist\Cangai_Portable\
echo ============================================================
echo.

REM ---- 1. Check PyInstaller ----
".venv\Scripts\python.exe" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [Install] PyInstaller not found, installing...
    ".venv\Scripts\python.exe" -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] PyInstaller install failed
        pause
        exit /b 1
    )
    echo [OK] PyInstaller installed
    echo.
)

REM ---- 2. Clean old output ----
if exist build rmdir /s /q build
if exist "dist\Cangai_Portable" rmdir /s /q "dist\Cangai_Portable"
if exist Cangai.spec del /q Cangai.spec

REM ---- 3. Build (onedir: fast startup) ----
REM ע�⣺���� --exclude-module unittest ���� numpy.testing ģ�鼶 import unittest��
REM �ų��˻ᵼ�´����ĳ�������������ModuleNotFoundError����
echo [Build] Generating portable folder...
REM NOTE: plugins are intentionally NOT packaged. Deliver the plugins folder
REM       separately; at runtime plugins load from %APPDATA%\Cangai\plugins\
REM       (or a plugins folder placed next to the exe).
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --name Cangai --icon "icons\Cangai.ico" --add-data "icons;icons" --add-data "resources;resources" --paths "deps\pymillcam-main\src" --add-data "deps\pymillcam-main\src\pymillcam;pymillcam" --paths "deps\build123d-0.11.1\src" --collect-submodules "build123d" --add-data "deps\build123d-0.11.1\src\build123d\data;build123d\data" --add-data ".venv\Lib\site-packages\lib3mf\lib3mf.dll;lib3mf" --hidden-import "pymillcam" --hidden-import "shapely" --hidden-import "pydantic" --collect-binaries "shapely" --collect-submodules "pydantic" --hidden-import "PySide6" --hidden-import "PySide6.QtWidgets" --hidden-import "PySide6.QtCore" --hidden-import "PySide6.QtGui" --hidden-import "PySide6.QtOpenGL" --hidden-import "numpy" --hidden-import "moderngl" --hidden-import "glcontext" --hidden-import "fontTools" --hidden-import "ezdxf" --exclude-module pytest --exclude-module tkinter --exclude-module test --exclude-module tests --exclude-module __pycache__ main.py

if errorlevel 1 (
    echo [ERROR] Build failed
    pause
    exit /b 1
)

REM ---- 4. Rename output + copy icon ----
if exist dist\Cangai (
    ren "dist\Cangai" "Cangai_Portable"
    copy /y "icons\Cangai.ico" "dist\Cangai_Portable\Cangai.ico" >nul
    REM Ship an empty plugins folder next to the exe so users can drop in
    REM their own plugins (resolved by builtinPluginDir in core/api.py).
    if not exist "dist\Cangai_Portable\plugins" mkdir "dist\Cangai_Portable\plugins"
    >  "dist\Cangai_Portable\plugins\README.txt" echo Drop your own plugins into this folder: a folder containing plugin.json, or a single .py file. Restart the app to load them.
    echo [OK] Output: dist\Cangai_Portable\
    echo.
    echo   Usage: open dist\Cangai_Portable\ folder,
    echo          double-click Cangai.exe to run - no install needed.
    echo          Copy the whole folder anywhere - it is portable.
) else (
    echo [ERROR] Output directory not found
    pause
    exit /b 1
)

REM ---- 5. Show size ----
echo.
echo ---- Output size ----
powershell -NoProfile -Command "$p='dist\Cangai_Portable'; $s=(Get-ChildItem -Recurse -Path $p | Measure-Object -Property Length -Sum).Sum; '{0:N1} MB' -f ($s/1MB)"
echo.
echo ============================================================
echo   Portable build complete!
echo ============================================================
pause
