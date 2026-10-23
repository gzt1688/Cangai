"""全局 QSS 主题与颜色常量，供各控件统一引用。

视觉风格：Windows 11 扁平化（浅灰窗体 + 白色面板 + 4px 圆角 + 细边框 +
浅色悬停 + 主题青色点缀）。只影响外观，不改变任何控件行为。
"""
from __future__ import annotations

from core.vector.geometry_types import ObjectType


PRIMARY = "#15b6c9"
PRIMARY_DARK = "#0e8a99"
PRIMARY_LIGHT = "#7fdce8"
TEXT_ON_PRIMARY = "#ffffff"
BG = "#f3f3f3"            # 窗体底（Win11 浅灰）
BG_PANEL = "#ffffff"      # 面板/输入框/列表白底
BG_VIEWPORT = "#f4f4f4"   # 画布/视口背景（浅灰）
TEXT = "#1a1a1a"
TEXT_DIM = "#5f5f5f"
BORDER = "#e0e0e0"        # 细分隔线（Win11 极浅）
SELECTION = "#15b6c9"
# 对象选中高亮色（设置-外观可配置）：曲线 / 浮雕高度场网格
SELECTION_CURVE = "#e20000"
SELECTION_MESH = "#ff6400"
GRID_MAJOR = "#c8c8c8"
GRID_MINOR = "#e8e8e8"
ORANGE = "#ff6400"
# 常用工具栏分组颜色（第1~5组，以分割线分组，第5组暂为备用）
QUICK_GROUP_COLORS = ["#3498db", "#e67e22", "#8e44ad", "#27ae60", "#e74c3c"]

FONT_FAMILY = "Microsoft YaHei"
FONT_SIZE = 10

# 中性交互色（Win11 语义）
HOVER = "#ebebeb"          # 中性悬停
PRESSED = "#e2e2e2"        # 中性按下
INPUT_BORDER = "#d6d6d6"   # 输入控件边框
ACCENT_TINT = "#e9f7f9"    # 主题色浅底（悬停点缀）


# 未来若高度场网格加入 ObjectType（如 MESH），在此登记即可
_MESH_TYPES = frozenset({ObjectType.SURFACE, ObjectType.IMAGE})


def selectionColorFor(obj) -> str:
    """对象选中高亮色：浮雕高度场网格用 SELECTION_MESH，其余（曲线等）用 SELECTION_CURVE。"""
    if obj.type() in _MESH_TYPES:
        return SELECTION_MESH
    return SELECTION_CURVE


def globalQss() -> str:
    # 勾选标记图标（QSS url 需正斜杠路径）
    from resource_path import resource_path
    import os
    checkUrl = resource_path("icons/check.svg").replace(os.sep, "/")
    return f"""
    * {{
        font-family: "{FONT_FAMILY}";
        font-size: {FONT_SIZE}pt;
        color: {TEXT};
    }}
    QMainWindow, QWidget {{ background: {BG}; }}
    /* QLabel 作为 QWidget 子类会命中上一条背景规则，导致每行文字出现
       灰色背景框；纯文本控件统一透明，沿用父容器底色。 */
    QLabel {{ background: transparent; }}
    QMenuBar {{ background: {BG}; border-bottom: 1px solid {BORDER}; padding: 2px; }}
    QMenuBar::item {{ padding: 3px 10px; background: transparent; border-radius: 4px; margin: 1px 2px; }}
    QMenuBar::item:selected {{ background: {HOVER}; color: {TEXT}; }}
    QMenu {{ background: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}
    QMenu::item {{ padding: 4px 20px; border-radius: 4px; margin: 1px 3px; background: transparent; }}
    QMenu::item:selected {{ background: {ACCENT_TINT}; color: {TEXT}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
    QToolBar {{ background: {BG}; border: none; spacing: 2px; padding: 2px; }}
    QToolBar QToolButton {{ padding: 3px 6px; border: none; border-radius: 4px; }}
    QToolBar QToolButton:hover {{ background: {HOVER}; }}
    QToolBar QToolButton:checked {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; }}
    QPushButton {{ background: {BG_PANEL}; color: {TEXT}; border: 1px solid {INPUT_BORDER}; border-radius: 4px; padding: 3px 10px; }}
    QPushButton:hover {{ background: #f7f7f7; border-color: #c4c4c4; }}
    QPushButton:pressed {{ background: {PRESSED}; }}
    QPushButton:disabled {{ background: #f6f6f6; color: #9d9d9d; border-color: #e5e5e5; }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {BG_PANEL}; border: 1px solid {INPUT_BORDER}; border-radius: 4px; padding: 1px 6px; }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border: 1px solid {PRIMARY}; background: {BG_PANEL}; }}
    QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: #c4c4c4; }}
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
        background: #f2f2f2; color: #b5b5b5; border-color: #e3e3e3;
    }}
    QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled {{ color: #b5b5b5; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QListWidget, QTreeWidget, QTextEdit, QTextBrowser {{ background: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 4px; }}
    QListWidget::item {{ border-radius: 4px; margin: 1px 2px; padding: 2px 4px; }}
    QListWidget::item:hover, QTreeWidget::item:hover {{ background: {HOVER}; }}
    QListWidget::item:selected, QTreeWidget::item:selected {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; }}
    QHeaderView::section {{ background: {BG}; border: none; border-bottom: 1px solid {BORDER}; border-right: 1px solid {BORDER}; padding: 4px; }}
    QStatusBar {{ background: {BG}; border-top: 1px solid {BORDER}; }}
    QStatusBar QLabel {{ padding: 0 4px; }}
    QCheckBox {{ spacing: 4px; background: transparent; }}
    QRadioButton {{ spacing: 4px; background: transparent; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 15px; height: 15px;
        border: 1px solid #b9b9b9; background: {BG_PANEL};
    }}
    QCheckBox::indicator {{ border-radius: 3px; }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {PRIMARY}; }}
    QCheckBox::indicator:checked {{
        background: {PRIMARY}; border-color: {PRIMARY};
        image: url({checkUrl});
    }}
    QRadioButton::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY}; }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        background: #f1f1f1; border-color: #dcdcdc;
    }}
    QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 4px; }}
    QTabBar::tab {{ background: transparent; color: {TEXT_DIM}; padding: 3px 14px; border: none;
                   border-bottom: 2px solid transparent; margin: 0 2px; }}
    QTabBar::tab:hover {{ color: {TEXT}; }}
    QTabBar::tab:selected {{ color: {PRIMARY_DARK}; border-bottom: 2px solid {PRIMARY}; font-weight: bold; }}
    QSplitter::handle {{ background: {BORDER}; }}
    QSplitter::handle:hover {{ background: {PRIMARY}; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: #c9c9c9; border-radius: 4px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: #a8a8a8; }}
    QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: #c9c9c9; border-radius: 4px; min-width: 24px; }}
    QScrollBar::handle:horizontal:hover {{ background: #a8a8a8; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ border: none; background: none; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
    QToolTip {{ background: {BG_PANEL}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 4px 8px; }}
    """


def dockManagerQss() -> str:
    return f"""
    ads--CDockManager {{
        background: {BG};
    }}
    ads--CDockWidgetTab {{
        background: transparent;
        color: {TEXT_DIM};
        padding: 2px 6px;
        border: none;
        border-bottom: 2px solid transparent;
        border-radius: 4px;
    }}
    ads--CDockWidgetTab:hover {{
        background: {HOVER};
    }}
    ads--CDockWidgetTab[activeTab="true"] {{
        background: {BG_PANEL};
        color: {PRIMARY_DARK};
        font-weight: bold;
        border-bottom: 2px solid {PRIMARY};
    }}
    ads--CDockAreaTabBar {{
        background: transparent;
    }}
    ads--CDockAreaTitleBar {{
        background: transparent;
        border-bottom: 1px solid {BORDER};
    }}
    ads--CElidingLabel {{
        background: transparent;
        color: {PRIMARY_DARK};
        font-weight: bold;
        padding: 0px 2px;
    }}
    ads--CSpacerWidget {{
        background: transparent;
    }}
    ads--CTitleBarButton {{
        max-width: 0px;
        min-width: 0px;
        max-height: 0px;
        min-height: 0px;
        border: none;
        margin: 0;
        padding: 0;
        background: transparent;
    }}
    ads--CDockAreaWidget {{
        background: {BG_PANEL};
    }}
    ads--CDockSplitter::handle {{
        background: {BORDER};
    }}
    ads--CDockSplitter::handle:hover {{
        background: {PRIMARY};
    }}
    """
