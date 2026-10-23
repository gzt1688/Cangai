# -*- coding: utf-8 -*-
"""Windows 文件关联：把 .cai 设为 Cangai 工程文件，双击即用本程序打开。

绿色版（免安装）不写注册表，所以资源管理器里 .cai 没有专属图标。
这里只写 **HKCU\\Software\\Classes**（当前用户），不需要管理员权限，
也不影响系统里其它用户和已安装版本。
"""
from __future__ import annotations

import os
import sys

APP_PROGID = "Cangai.Document"
APP_EXT = ".cai"
APP_DESC = "Cangai 工程文件"
APP_MIME = "application/x-cangai"


def _main_py() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def _ico_path() -> str:
    """图标优先用 exe 同目录的 Cangai.ico（绿色版打包会复制一份），
    没有就退回 exe 内嵌图标。"""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for p in (os.path.join(exe_dir, "Cangai.ico"),
              os.path.join(exe_dir, "icons", "Cangai.ico")):
        if os.path.exists(p):
            return p
    try:
        from resource_path import resource_path
        p = resource_path("icons/Cangai.ico")
        if os.path.exists(p):
            return p
    except Exception:
        pass
    return ""


def install_target() -> tuple[str, str]:
    """返回 (shell 打开命令, DefaultIcon 值)。"""
    exe = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        cmd = f'"{exe}" "%1"'
    else:
        # 源码运行：登记 python + main.py，便于开发时也能验证
        cmd = f'"{exe}" "{_main_py()}" "%1"'
    ico = _ico_path()
    icon = f'"{ico}"' if ico else f'"{exe}",0'
    return cmd, icon


def _winreg():
    import winreg
    return winreg


def current_command() -> str:
    """读取 HKCU 下已登记的打开命令（未登记返回空串）。"""
    if not sys.platform.startswith("win"):
        return ""
    wr = _winreg()
    path = rf"Software\Classes\{APP_PROGID}\shell\open\command"
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, path) as k:
            return wr.QueryValueEx(k, "")[0]
    except OSError:
        return ""


def is_registered() -> bool:
    """当前用户下 .cai 是否已指向本程序（且命令与本机路径一致）。"""
    if not sys.platform.startswith("win"):
        return False
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, r"Software\Classes\.cai") as k:
            prog = wr.QueryValueEx(k, "")[0]
    except OSError:
        return False
    if prog != APP_PROGID:
        return False
    cmd, _ = install_target()
    return os.path.normcase(current_command()) == os.path.normcase(cmd)


def user_choice_progid() -> str:
    """Windows 10/11 的"默认应用"记录（UserChoice）。
    存在且不是本程序时，系统会继续用旧程序打开，需要用户在系统设置里改。"""
    if not sys.platform.startswith("win"):
        return ""
    wr = _winreg()
    path = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{APP_EXT}\UserChoice"
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, path) as k:
            return wr.QueryValueEx(k, "ProgId")[0]
    except OSError:
        return ""


def _notify_shell() -> None:
    """通知资源管理器刷新文件类型关联与图标缓存。"""
    try:
        import ctypes
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except Exception:
        pass


def register() -> None:
    """把 .cai 关联到本程序（写 HKCU，幂等，可重复调用）。"""
    wr = _winreg()
    cmd, icon = install_target()
    hkcu = wr.HKEY_CURRENT_USER

    def _set(key_path: str, name: str, value: str) -> None:
        with wr.CreateKey(hkcu, key_path) as k:
            if name:
                wr.SetValueEx(k, name, 0, wr.REG_SZ, value)
            else:
                wr.SetValue(k, "", wr.REG_SZ, value)

    _set(rf"Software\Classes\{APP_EXT}", "", APP_PROGID)
    _set(rf"Software\Classes\{APP_EXT}", "Content Type", APP_MIME)
    _set(rf"Software\Classes\{APP_EXT}\OpenWithProgids", APP_PROGID, "")
    _set(rf"Software\Classes\{APP_PROGID}", "", APP_DESC)
    _set(rf"Software\Classes\{APP_PROGID}", "FriendlyTypeName", APP_DESC)
    _set(rf"Software\Classes\{APP_PROGID}\SupportedTypes", APP_EXT, "")
    _set(rf"Software\Classes\{APP_PROGID}\DefaultIcon", "", icon)
    _set(rf"Software\Classes\{APP_PROGID}\shell\open", "", "&Open")
    _set(rf"Software\Classes\{APP_PROGID}\shell\open\command", "", cmd)
    # 让"打开方式"对话框里也能列出本程序
    exe_name = os.path.basename(os.path.abspath(sys.executable))
    _set(rf"Software\Classes\Applications\{exe_name}\shell\open\command", "", cmd)
    _set(rf"Software\Classes\Applications\{exe_name}\SupportedTypes", APP_EXT, "")
    _notify_shell()
