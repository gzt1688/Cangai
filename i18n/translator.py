"""轻量 i18n 翻译器：中文即 key，英文字典查表。

用法：
    from i18n import tr
    btn = QPushButton(tr("画布"))

切换语言：
    from i18n import setLanguage
    setLanguage("English")   # 触发 languageChanged 信号

组件实现 retranslateUi() 后，连接 languageChanged 信号即可实时刷新。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from . import strings_en

_current = "简体中文"
_table = strings_en.TABLE   # 中文 -> 英文


class _Translator(QObject):
    languageChanged = Signal()

    def current(self) -> str:
        return _current


_inst = _Translator()


def tr(text: str) -> str:
    """翻译：英文模式查表，命中返回英文；未命中或中文模式返回原中文。"""
    if _current == "English":
        return _table.get(text, text)
    return text


def setLanguage(lang: str) -> None:
    """切换语言并广播信号（lang 取自 app_settings.LANGUAGES）。"""
    global _current
    if lang == _current:
        return
    _current = lang
    _inst.languageChanged.emit()


def getLanguage() -> str:
    return _current


def languageChanged() -> Signal:
    """语言变更信号（组件连接此信号实现 retranslateUi 实时刷新）。"""
    return _inst.languageChanged


def translatorInstance() -> _Translator:
    return _inst