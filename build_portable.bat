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
REM Delete BOTH output names and refuse to continue when the old folder survives:
REM PyInstaller always writes to dist\Cangai, and step 4 renames it - a leftover
REM dist\Cangai_Portable makes that rename fail and leaves the real build in
REM dist\Cangai while plugins/mcp land in the stale folder (wrong folder shipped).
if exist build rmdir /s /q build
if exist "dist\Cangai" rmdir /s /q "dist\Cangai"
if exist "dist\Cangai_Portable" rmdir /s /q "dist\Cangai_Portable"
if exist "dist\Cangai_Portable" (
    echo [ERROR] cannot delete old dist\Cangai_Portable
    echo         Close Cangai.exe and any Explorer window inside it, then retry.
    pause
    exit /b 1
)
if exist Cangai.spec del /q Cangai.spec

REM ---- 3. Build (onedir: fast startup) ----
REM ע�⣺���� --exclude-module unittest ���� numpy.testing ģ�鼶 import unittest��
REM �ų��˻ᵼ�´����ĳ�������������ModuleNotFoundError����
echo [Build] Generating portable folder...
REM NOTE: plugins are never frozen into the exe - builtinPluginDir() resolves to
REM       the "plugins" folder next to the exe. Step 4 copies the ai_lineart
REM       plugin CODE in (tens of KB), but NEVER its models folder, and the AI
REM       runtime (onnxruntime/torch) is excluded on the command line above.
REM       WHY the explicit excludes: no module of the app imports torch any more,
REM       but scipy._external.array_api_compat.torch and
REM       sklearn.externals.array_api_compat.torch do (guarded by try/except at
REM       runtime). PyInstaller does not look at the guard, hits its official
REM       hook-torch and copied the whole CUDA build: _internal\torch was 3.9GB
REM       of a 4.8GB package. vtracer (the tracing engine, a standalone Rust
REM       wheel) stays in, so the packaged app still traces images to curves.
REM       resources/ is added file by file on purpose: envmaps/ holds 168MB of
REM       test HDRIs (97MB of them .exr, which this build cannot even decode);
REM       only the 1.5MB built-in studio_default.hdr belongs in the package.
REM       Other plugins still load from %APPDATA%\Cangai\plugins\ as before.
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --name Cangai --icon "icons\Cangai.ico" --add-data "icons;icons" --add-data "resources\fonts;resources\fonts" --add-data "resources\donate_qr.png;resources" --add-data "resources\splash_dragonfly.png;resources" --add-data "resources\envmaps\studio_default.hdr;resources\envmaps" --add-data "core\ai\models.json;core\ai" --paths "deps\pymillcam-main\src" --add-data "deps\pymillcam-main\src\pymillcam;pymillcam" --paths "deps\build123d-0.11.1\src" --collect-submodules "build123d" --add-data "deps\build123d-0.11.1\src\build123d\data;build123d\data" --add-data ".venv\Lib\site-packages\lib3mf\lib3mf.dll;lib3mf" --hidden-import "pymillcam" --hidden-import "shapely" --hidden-import "pydantic" --collect-binaries "shapely" --collect-submodules "pydantic" --hidden-import "PySide6" --hidden-import "PySide6.QtWidgets" --hidden-import "PySide6.QtCore" --hidden-import "PySide6.QtGui" --hidden-import "PySide6.QtOpenGL" --hidden-import "PySide6.QtNetwork" --hidden-import "numpy" --hidden-import "moderngl" --hidden-import "glcontext" --hidden-import "fontTools" --hidden-import "ezdxf" --exclude-module pytest --exclude-module tkinter --exclude-module test --exclude-module tests --exclude-module __pycache__ --exclude-module torch --exclude-module torchvision --exclude-module torchaudio --exclude-module functorch --exclude-module controlnet_aux --exclude-module timm --exclude-module einops --exclude-module huggingface_hub --exclude-module transformers --exclude-module safetensors --exclude-module onnxruntime --exclude-module onnx --exclude-module skimage --exclude-module scikit-image --hidden-import "vtracer" main.py

if errorlevel 1 (
    echo [ERROR] Build failed
    pause
    exit /b 1
)

REM ---- 4. Rename output + copy icon ----
if exist dist\Cangai (
    if exist "dist\Cangai_Portable" rmdir /s /q "dist\Cangai_Portable"
    ren "dist\Cangai" "Cangai_Portable"
    if not exist "dist\Cangai_Portable\Cangai.exe" (
        echo [ERROR] rename to Cangai_Portable failed - build output is in dist\Cangai
        pause
        exit /b 1
    )
    copy /y "icons\Cangai.ico" "dist\Cangai_Portable\Cangai.ico" >nul
    REM Ship an empty plugins folder next to the exe so users can drop in
    REM their own plugins (resolved by builtinPluginDir in core/api.py).
    if not exist "dist\Cangai_Portable\plugins" mkdir "dist\Cangai_Portable\plugins"
    >  "dist\Cangai_Portable\plugins\README.txt" echo Drop your own plugins into this folder: a folder containing plugin.json, or a single .py file. Restart the app to load them.
    REM Ship the AI line-art plugin as loose files (code + panel + pipeline),
    REM explicitly excluding its models folder - that is the 487MB of weights.
    if exist "plugins_private\ai_lineart\plugin.json" (
        robocopy "plugins_private\ai_lineart" "dist\Cangai_Portable\plugins\ai_lineart" /E /XD models __pycache__ .git /NFL /NDL /NJH /NJS /NP >nul
        if errorlevel 8 (echo [WARN] ai_lineart plugin copy failed) else (echo [OK] plugin ai_lineart copied - code only, no models)
    ) else (
        echo [WARN] plugins_private\ai_lineart not found, AI line-art will be missing
    )
    REM Ship the MCP bridge script (external AI clients drive the app via it)
    xcopy /y /i /q "mcp" "dist\Cangai_Portable\mcp" >nul
    echo [OK] Output: dist\Cangai_Portable\
    echo.
    echo   Usage: open dist\Cangai_Portable\ folder,
    echo          double-click Cangai.exe to run - no install needed.
    echo          Copy the whole folder anywhere - it is portable.
    echo.
    echo   AI line-art models are NOT in this package. Build them with
    echo          .venv\Scripts\python.exe pack_models.py
    echo   then send / copy the resulting ai_models.zip into the folder that
    echo   holds Cangai.exe. It is unpacked once into %%APPDATA%%\Cangai\ai_models.
    if exist "plugins_private\ai_lineart\ai_models_README.txt" (
        copy /y "plugins_private\ai_lineart\ai_models_README.txt" "dist\Cangai_Portable\ai_models_README.txt" >nul
    )
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
