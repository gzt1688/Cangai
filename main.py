"""Cangai-Relief 浮雕设计软件入口。创建 QApplication 并启动主窗口。"""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 崩溃诊断：段错误（原生闪退）时把所有线程的 Python 栈写入 crash.log，便于定位。
# 文件句柄需在整个进程生命周期内保持打开，故用模块级全局持有。
import faulthandler

_CRASH_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")
try:
    _CRASH_LOG_FILE = open(_CRASH_LOG_PATH, "w", encoding="utf-8")
    _CRASH_LOG_FILE.write("=== faulthandler enabled ===\n")
    _CRASH_LOG_FILE.flush()
    faulthandler.enable(file=_CRASH_LOG_FILE, all_threads=True)
except Exception:
    _CRASH_LOG_FILE = None
    faulthandler.enable(all_threads=True)

from PySide6.QtWidgets import QApplication, QStyleFactory
from PySide6.QtCore import Qt

from ui.windows.mainWindow import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    # 注意：Qt 6 / PySide6 默认已开启高 DPI 支持，不必再设置
    # AA_UseHighDpiPixmaps（该属性在 Qt 6.5+ 被标记为 deprecated，
    # 调用 setAttribute 会触发 DeprecationWarning，这里彻底跳过）。
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
