# -*- coding: utf-8 -*-
"""临时验证：设置对话框（自动保存接线/取消回滚/脏信号）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

import core.app_settings as app_settings
import ui.styles.theme as theme
from ui.widgets.settings_dialog import SettingsDialog

app = QApplication(sys.argv)
fails = []


def check(name, ok):
    print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append(name)


dlg = SettingsDialog()

# ---- 1) 文件页控件接线 ----
check("自动保存初值回填", dlg._autosaveCheck.isChecked() == bool(app_settings.autosave_enabled))
dlg._autosaveCheck.setChecked(True)
check("自动保存开关写入设置", app_settings.autosave_enabled is True)
dlg._intervalSpin.setValue(9)
check("自动保存间隔写入设置", app_settings.autosave_interval_min == 9)
dlg._backupCheck.setChecked(False)
check("备份开关写入设置", app_settings.backup_on_save is False)

# ---- 2) 脏信号：无改动应用 → 不发任何类别信号 ----
emitted = []
dlg.appearanceChanged.connect(lambda: emitted.append("appearance"))
dlg.fileSlotsChanged.connect(lambda: emitted.append("slots"))
dlg.renderSettingsChanged.connect(lambda: emitted.append("render"))
dlg.dimensionSettingsChanged.connect(lambda: emitted.append("dim"))

dlg2 = SettingsDialog()   # 全新对话框（无任何改动）
dlg2.appearanceChanged.connect(lambda: emitted.append("appearance2"))
dlg2.fileSlotsChanged.connect(lambda: emitted.append("slots2"))
dlg2.renderSettingsChanged.connect(lambda: emitted.append("render2"))
dlg2.dimensionSettingsChanged.connect(lambda: emitted.append("dim2"))
dlg2._onApply()
check("无改动应用不发信号", emitted == [])

# ---- 3) 有改动应用 → 只发对应类别 ----
dlg2._onFontChanged(14)
emitted.clear()
dlg2._onApply()
check("改字体只发外观信号", emitted == ["appearance2"])

# ---- 4) 取消回滚主题色与文件设置 ----
oldPrimary = theme.PRIMARY
# 重置到默认值再创建对话框：快照基线 = False/5/True
app_settings.autosave_enabled = False
app_settings.autosave_interval_min = 5
app_settings.backup_on_save = True
dlg3 = SettingsDialog()
theme.PRIMARY = "#123456"
theme.SELECTION_CURVE = "#654321"
app_settings.autosave_enabled = True       # 创建后再改动
app_settings.autosave_interval_min = 20
app_settings.backup_on_save = False
dlg3._onCancel()   # 恢复快照并广播
check("取消回滚主题色", theme.PRIMARY == oldPrimary)
check("取消回滚选中色", theme.SELECTION_CURVE != "#654321")
check("取消回滚文件设置", app_settings.autosave_enabled is False
      and app_settings.autosave_interval_min == 5
      and app_settings.backup_on_save is True)

print("=== 设置对话框测试 ===")
print("=== 全部通过 ===" if not fails else f"=== 失败 {fails} ===")
sys.exit(1 if fails else 0)
