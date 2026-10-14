@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   Cangai Installer Build (no 3D mode at runtime)
echo   Output: dist\Cangai_Setup.exe
echo   Default install path: D:\Cangai
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

REM ---- 2. Check Inno Setup (required to build installer) ----
set "ISSC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISSC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISSC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not defined ISSC (
    for /f "delims=" %%I in ('where iscc 2^>nul') do set "ISSC=%%I"
)
if not defined ISSC (
    echo [WARN] Inno Setup 6 not found, cannot build installer.exe
    echo        Install Inno Setup 6 - free: https://jrsoftware.org/isdl.php
    echo        Will only build single-file exe (onefile) as fallback.
    echo        Re-run this BAT after installing Inno Setup to get full installer.
    echo.
    set "NO_INNO=1"
) else (
    echo [Check] Inno Setup: !ISSC!
    set "NO_INNO=0"
)
echo.

REM ---- 3. Clean old output ----
if exist build rmdir /s /q build
if exist "dist\Cangai.exe" del /q "dist\Cangai.exe"
if exist Cangai.spec del /q Cangai.spec

REM ---- 4. Build (onefile: single exe) ----
REM 注意：不能 --exclude-module unittest —— numpy.testing 模块级 import unittest，
REM 排除了会导致打包后的程序启动即崩（ModuleNotFoundError）。
echo [Build] Generating single-file exe...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name Cangai --icon "icons\Cangai.ico" --add-data "icons;icons" --add-data "resources;resources" --add-data "plugins;plugins" --paths "deps\pymillcam-main\src" --add-data "deps\pymillcam-main\src\pymillcam;pymillcam" --hidden-import "pymillcam" --hidden-import "shapely" --hidden-import "pydantic" --collect-binaries "shapely" --collect-submodules "pydantic" --hidden-import "PySide6" --hidden-import "PySide6.QtWidgets" --hidden-import "PySide6.QtCore" --hidden-import "PySide6.QtGui" --hidden-import "PySide6.QtOpenGL" --hidden-import "numpy" --hidden-import "moderngl" --hidden-import "glcontext" --hidden-import "fontTools" --hidden-import "ezdxf" --exclude-module pytest --exclude-module tkinter --exclude-module test --exclude-module tests --exclude-module __pycache__ main.py

if errorlevel 1 (
    echo [ERROR] Build failed
    pause
    exit /b 1
)

echo [OK] Single-file exe: dist\Cangai.exe
echo.

REM ---- 5. Build installer with Inno Setup ----
if "%NO_INNO%"=="1" (
    echo ============================================================
    echo   Single-file exe done - no Inno Setup, installer skipped
    echo   Output: dist\Cangai.exe
    echo   Install Inno Setup 6 and re-run to get full installer
    echo ============================================================
    powershell -NoProfile -Command "$p='dist\Cangai.exe'; $s=(Get-Item $p).Length; '{0:N1} MB' -f ($s/1MB)"
    pause
    exit /b 0
)

echo [Gen] Writing Inno Setup script...
call :write_iss
if errorlevel 1 (
    echo [ERROR] Failed to write .iss
    pause
    exit /b 1
)

echo [Compile] Running Inno Setup...
"!ISSC!" /Q "installer.iss"
if errorlevel 1 (
    echo [WARN] Inno Setup compile failed, check errors above
    echo        Use single-file exe directly: dist\Cangai.exe
    pause
    exit /b 1
)

if exist "dist\Cangai_Setup.exe" (
    del /q installer.iss
    echo.
    echo ============================================================
    echo   Installer build complete!
    echo   Output: dist\Cangai_Setup.exe
    echo   Default install path: D:\Cangai
    echo ============================================================
    powershell -NoProfile -Command "$p='dist\Cangai_Setup.exe'; $s=(Get-Item $p).Length; '{0:N1} MB' -f ($s/1MB)"
) else (
    echo [WARN] installer not found, check Inno Setup
)
pause
exit /b 0

REM ============================================================
REM Sub: write Inno Setup script installer.iss
REM ============================================================
:write_iss
(
echo ; Cangai installer script (Inno Setup 6)
echo #define MyAppName "Cangai"
echo #define MyAppExeName "Cangai.exe"
echo #define MyAppVersion "1.0.0"
echo #define MyAppPublisher "Cangai"
echo.
echo [Setup]
echo AppName={#MyAppName}
echo AppVersion={#MyAppVersion}
echo AppPublisher={#MyAppPublisher}
echo DefaultDirName=D:\Cangai
echo DefaultGroupName=Cangai
echo UninstallDisplayIcon={app}\{#MyAppExeName}
echo OutputDir=dist
echo OutputBaseFilename=Cangai_Setup
echo SetupIconFile=icons\Cangai.ico
echo Compression=lzma2/ultra
echo SolidCompression=yes
echo PrivilegesRequired=lowest
echo DisableProgramGroupPage=yes
echo AllowNoIcons=yes
echo ArchitecturesAllowed=x64
echo ArchitecturesInstallIn64BitMode=x64
echo WizardStyle=modern
echo.
echo [Languages]
echo Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
echo Name: "english"; MessagesFile: "compiler:Default.isl"
echo.
echo [Tasks]
echo Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
echo Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
echo.
echo [Files]
echo Source: "dist\Cangai.exe"; DestDir: "{app}"; Flags: ignoreversion
echo Source: "icons\Cangai.ico"; DestDir: "{app}"; Flags: ignoreversion
echo Source: "icons\*"; DestDir: "{app}\icons"; Flags: ignoreversion recursesubdirs createallsubdirs
echo Source: "resources\*"; DestDir: "{app}\resources"; Flags: ignoreversion recursesubdirs createallsubdirs
echo Source: "plugins\*"; DestDir: "{app}\plugins"; Flags: ignoreversion recursesubdirs createallsubdirs
echo.
echo [Icons]
echo Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Cangai.ico"
echo Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
echo Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Cangai.ico"; Tasks: desktopicon
echo Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon
echo.
echo [Run]
echo Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
echo.
echo [UninstallDelete]
echo Type: filesandordirs; Name: "{app}"
) > installer.iss
exit /b 0
