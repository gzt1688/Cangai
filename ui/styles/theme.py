"""全局 QSS 主题与颜色常量，供各控件统一引用。"""
from __future__ import annotations

from core.vector.geometry_types import ObjectType


PRIMARY = "#15b6c9"
PRIMARY_DARK = "#0e8a99"
PRIMARY_LIGHT = "#7fdce8"
TEXT_ON_PRIMARY = "#ffffff"
BG = "#f0f0f0"
BG_PANEL = "#ffffff"
BG_VIEWPORT = "#fafafa"
TEXT = "#1a1a1a"
TEXT_DIM = "#666666"
BORDER = "#d0d0d0"
SELECTION = "#15b6c9"
# 对象选中高亮色（设置-外观可配置）：曲线 / 浮雕高度场网格
SELECTION_CURVE = "#15b6c9"
SELECTION_MESH = "#ff6400"
GRID_MAJOR = "#c8c8c8"
GRID_MINOR = "#e8e8e8"
ORANGE = "#ff6400"
# 常用工具栏分组颜色（第1~5组，以分割线分组，第5组暂为备用）
QUICK_GROUP_COLORS = ["#3498db", "#e67e22", "#8e44ad", "#27ae60", "#e74c3c"]

FONT_FAMILY = "Microsoft YaHei"
FONT_SIZE = 10


# 未来若高度场网格加入 ObjectType（如 MESH），在此登记即可
_MESH_TYPES = frozenset({ObjectType.SURFACE, ObjectType.IMAGE})


def selectionColorFor(obj) -> str:
    """对象选中高亮色：浮雕高度场网格用 SELECTION_MESH，其余（曲线等）用 SELECTION_CURVE。"""
    if obj.type() in _MESH_TYPES:
        return SELECTION_MESH
    return SELECTION_CURVE


def globalQss() -> str:
    return f"""
    * {{
        font-family: "{FONT_FAMILY}";
        font-size: {FONT_SIZE}pt;
        color: {TEXT};
    }}
    QMainWindow, QWidget {{ background: {BG}; }}
    QMenuBar {{ background: #e6e6e6; border-bottom: 1px solid {BORDER}; padding: 1px; }}
    QMenuBar::item {{ padding: 2px 8px; background: transparent; }}
    QMenuBar::item:selected {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; }}
    QMenu {{ background: {BG_PANEL}; border: 1px solid {BORDER}; padding: 2px; }}
    QMenu::item {{ padding: 3px 18px; }}
    QMenu::item:selected {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; }}
    QToolBar {{ background: #e6e6e6; border: none; spacing: 1px; padding: 1px; }}
    QToolBar QToolButton {{ padding: 2px 4px; border: none; border-radius: 2px; }}
    QToolBar QToolButton:hover {{ background: #d0d0d0; }}
    QToolBar QToolButton:checked {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; }}
    QPushButton {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; border: none; border-radius: 2px; padding: 0px; }}
    QPushButton:hover {{ background: {PRIMARY_DARK}; }}
    QPushButton:pressed {{ background: {PRIMARY_DARK}; }}
    QPushButton:disabled {{ background: #b0b0b0; color: #e0e0e0; }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 2px; padding: 1px 3px; }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border: 1px solid {PRIMARY}; }}
    QListWidget, QTreeWidget, QTextEdit, QTextBrowser {{ background: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 2px; }}
    QListWidget::item:selected, QTreeWidget::item:selected {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; }}
    QHeaderView::section {{ background: #e6e6e6; border: none; border-right: 1px solid {BORDER}; padding: 2px; }}
    QStatusBar {{ background: #e6e6e6; border-top: 1px solid {BORDER}; }}
    QStatusBar QLabel {{ padding: 0 4px; }}
    QCheckBox {{ spacing: 4px; }}
    QTabWidget::pane {{ border: 1px solid {BORDER}; }}
    QTabBar::tab {{ background: #e0e0e0; padding: 2px 10px; border: 1px solid {BORDER}; margin: 0; }}
    QTabBar::tab:selected {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; border: none; }}
    QSplitter::handle {{ background: {BORDER}; }}
    QSplitter::handle:hover {{ background: {PRIMARY}; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; }}
    QScrollBar::handle:vertical {{ background: #c0c0c0; border-radius: 4px; min-height: 20px; }}
    QScrollBar::handle:vertical:hover {{ background: {PRIMARY}; }}
    QScrollBar:horizontal {{ background: transparent; height: 8px; }}
    QScrollBar::handle:horizontal {{ background: #c0c0c0; border-radius: 4px; min-width: 20px; }}
    QScrollBar::handle:horizontal:hover {{ background: {PRIMARY}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ border: none; background: none; }}
    QToolTip {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; border: none; padding: 2px 4px; }}
    """


def dockManagerQss() -> str:
    return f"""
    ads--CDockManager {{
        background: {BG};
    }}
    ads--CDockWidgetTab {{
        background: transparent;
        color: #333333;
        padding: 0px 1px;
        border: none;
        border-radius: 2px;
    }}
    ads--CDockWidgetTab[activeTab="true"] {{
        background: {PRIMARY};
        color: {TEXT_ON_PRIMARY};
        font-weight: bold;
    }}
    ads--CDockAreaTabBar {{
        background: transparent;
    }}
    ads--CDockAreaTitleBar {{
        background: transparent;
    }}
    ads--CElidingLabel {{
        background: {PRIMARY};
        color: {TEXT_ON_PRIMARY};
        padding: 0px 1px;
        border-radius: 2px;
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