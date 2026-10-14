# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

binaries = []
hiddenimports = ['pymillcam', 'shapely', 'pydantic', 'PySide6', 'PySide6.QtWidgets', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtOpenGL', 'numpy', 'moderngl', 'glcontext', 'fontTools', 'ezdxf']
binaries += collect_dynamic_libs('shapely')
hiddenimports += collect_submodules('pydantic')


a = Analysis(
    ['main.py'],
    pathex=['deps/pymillcam-main/src'],
    binaries=binaries,
    datas=[('icons', 'icons'), ('resources', 'resources'), ('deps/pymillcam-main/src/pymillcam', 'pymillcam')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'tkinter', 'test', 'tests', '__pycache__'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Cangai',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icons/Cangai.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Cangai',
)
