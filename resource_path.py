"""资源路径解析：统一处理开发环境与 PyInstaller 打包后的差异。

打包后（PyInstaller onedir 6.x）所有 --add-data 资源位于 sys._MEIPASS
指向的 _internal 目录；开发环境资源目录是项目根目录（本文件所在目录）。
返回统一正斜杠分隔，QIcon / QSS url() 均兼容。
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", None) or _PROJECT_ROOT
    return os.path.join(base, rel).replace("\\", "/")
