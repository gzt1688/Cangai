"""Cangai 浮雕设计软件入口。创建 QApplication 并启动主窗口。"""
from __future__ import annotations
import sys
import os
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 注入 vendored pymillcam（CNC 模块复用其 core/engine/post，UI 自写）
# 开发环境：deps/pymillcam-main/src 下是 src-layout，可直接 import pymillcam
# 打包环境：Cangai.spec / build_*.bat 把 pymillcam 源码以 datas 解包到
#           _MEIPASS/pymillcam（onedir 下 _MEIPASS = dist\Cangai\_internal）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PYMILLCAM_SRC = os.path.join(_BASE_DIR, "deps", "pymillcam-main", "src")
if getattr(sys, "frozen", False):
    _MEIP = getattr(sys, "_MEIPASS", _BASE_DIR)
    _pm_pkg = os.path.join(_MEIP, "pymillcam")
    if os.path.isdir(_pm_pkg):
        _PYMILLCAM_SRC = os.path.dirname(_pm_pkg)
if os.path.isdir(_PYMILLCAM_SRC) and _PYMILLCAM_SRC not in sys.path:
    sys.path.insert(0, _PYMILLCAM_SRC)

# 崩溃诊断：段错误（原生闪退）时把所有线程的 Python 栈写入 crash.log，便于定位。
# 文件句柄需在整个进程生命周期内保持打开，故用模块级全局持有。
import faulthandler

_CRASH_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")
try:
    _CRASH_LOG_FILE = open(_CRASH_LOG_PATH, "a", encoding="utf-8")
    _CRASH_LOG_FILE.write("\n\n=== run at %s ===\n" % datetime.now().isoformat(timespec="seconds"))
    _CRASH_LOG_FILE.write("=== faulthandler enabled ===\n")
    _CRASH_LOG_FILE.flush()
    faulthandler.enable(file=_CRASH_LOG_FILE, all_threads=True)
except Exception:
    _CRASH_LOG_FILE = None
    faulthandler.enable(all_threads=True)

# ---- 全局 Python 未捕获异常兜底：写 crash.log + 控制台 ----
_original_excepthook = sys.excepthook


def _our_excepthook(exc_type, exc, tb):
    try:
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        stamp = datetime.now().isoformat(timespec="seconds")
        full = f"\n=== PY UNCAUGHT EXCEPTION @ {stamp} ===\n{msg}\n"
        print(full, file=sys.stderr, flush=True)
        if _CRASH_LOG_FILE is not None:
            try:
                _CRASH_LOG_FILE.write(full)
                _CRASH_LOG_FILE.flush()
            except Exception:
                pass
    except Exception:
        pass
    # 尽量走原来的钩子
    try:
        if _original_excepthook is not None:
            _original_excepthook(exc_type, exc, tb)
    except Exception:
        pass


sys.excepthook = _our_excepthook

# 针对 threading.Thread 里 run() 未捕获的异常 (PySide6/threading 不会走 sys.excepthook)：
import threading

_original_thread_run = threading.Thread.run


def _thread_run_patched(self):
    try:
        _original_thread_run(self)
    except Exception as _e:
        try:
            msg = traceback.format_exc()
            stamp = datetime.now().isoformat(timespec="seconds")
            full = f"\n=== THREAD UNCAUGHT @ {stamp} ({self.name}) ===\n{msg}\n"
            print(full, file=sys.stderr, flush=True)
            if _CRASH_LOG_FILE is not None:
                try:
                    _CRASH_LOG_FILE.write(full)
                    _CRASH_LOG_FILE.flush()
                except Exception:
                    pass
        except Exception:
            pass


threading.Thread.run = _thread_run_patched

# ---- Qt 消息（Warning / Critical / Fatal）兜底：写 crash.log ----
from PySide6.QtCore import (
    QtMsgType, qInstallMessageHandler, QMessageLogContext)


def _qt_msg_handler(msg_type: QtMsgType, ctx: QMessageLogContext, msg: str) -> None:
    try:
        if msg_type == QtMsgType.QtDebugMsg:
            return   # 不写 debug
        label = {
            QtMsgType.QtInfoMsg: "QT INFO",
            QtMsgType.QtWarningMsg: "QT WARN",
            QtMsgType.QtCriticalMsg: "QT CRITICAL",
            QtMsgType.QtFatalMsg: "QT FATAL",
        }.get(msg_type, "QT ???")
        stamp = datetime.now().isoformat(timespec="seconds")
        # Fatal / Critical 一定写入 crash.log
        if msg_type in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            loc = ""
            if ctx and (ctx.file or ctx.function):
                loc = f" @ {ctx.file or '?'}:{ctx.line or 0} {ctx.function or ''}"
            line = f"\n=== {label} @ {stamp}{loc} ===\n{msg}\n"
            print(line, file=sys.stderr, flush=True)
            if _CRASH_LOG_FILE is not None:
                try:
                    _CRASH_LOG_FILE.write(line)
                    _CRASH_LOG_FILE.flush()
                except Exception:
                    pass
    except Exception:
        pass


qInstallMessageHandler(_qt_msg_handler)

from PySide6.QtWidgets import QApplication, QStyleFactory
from PySide6.QtGui import QIcon
from resource_path import resource_path
from PySide6.QtCore import QTimer

from core import app_settings
from i18n import setLanguage, tr


def _startup_file_arg() -> str:
    """取命令行里要打开的工程文件（双击关联后 Windows 会传入路径）。"""
    for a in sys.argv[1:]:
        if a and not a.startswith("-") and os.path.isfile(a) \
                and a.lower().endswith((".cai", ".cag")):
            return os.path.abspath(a)
    return ""


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setWindowIcon(QIcon(resource_path("icons/Cangai.ico")))
    # 初始化界面语言（从持久化设置加载；默认简体中文）
    setLanguage(app_settings.language)
    # 注意：Qt 6 / PySide6 默认已开启高 DPI 支持，不必再设置
    # AA_UseHighDpiPixmaps（该属性在 Qt 6.5+ 被标记为 deprecated，
    # 调用 setAttribute 会触发 DeprecationWarning，这里彻底跳过）。

    # 启动加载页：先于主窗口显示并喂进度，所有重模块预热完成后才进入主界面
    from ui.widgets.splash import SplashScreen
    splash = SplashScreen()
    splash.setLoadProgress(2, tr("正在初始化界面…"))
    splash.show()
    app.processEvents()

    # 主窗口模块涉及较多 UI/几何导入，延后到加载页已显示之后再导入
    from ui.windows.mainWindow import MainWindow
    win = MainWindow()
    win.startupProgress.connect(splash.setLoadProgress)

    entered = {"done": False}

    def _enterMainWindow() -> None:
        if entered["done"]:
            return
        entered["done"] = True
        win.show()
        splash.finish(win)
        # 双击 .cai（文件关联）时命令行带路径：主窗口显示后载入该工程
        _pending = _startup_file_arg()
        if _pending:
            QTimer.singleShot(0, lambda: win._openFileAsDocument(_pending))
        # 3D 内核（build123d/OCP）已在加载页内预热完毕，这里不再重复触发；
        # 仅当加载页异常兜底提前进入时，_ensure3DKernel 才作为补位入口。

    win.startupFinished.connect(_enterMainWindow)
    # 兜底：预载线程若异常卡住，90 秒后强制进入主界面，避免永久停在加载页
    QTimer.singleShot(90_000, _enterMainWindow)
    win.beginStartupPreload()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
