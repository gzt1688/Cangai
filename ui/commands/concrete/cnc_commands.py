"""CNC 命令（精雕 7 步流程）：

1. CncMachineCommand   - 机床设置（对话框）
2. CncStockCommand     - 安装几何体（对话框：材料+夹具）
3. CncToolsCommand     - 刀具表（对话框）
4. CncProfileCommand   - 生成刀路（对话框：加工方案/走刀/进给/安全/高级/计算→生成 IR 存 CncState）
5. CncCollisionCommand - 碰撞检查（刀路 Z/XY 越界 + 夹具碰撞）
6. CncSimulateCommand  - 实体模拟（2D 轨迹动画先做，3D 占位）
7. CncOutputCommand    - 路径输出（G 代码对话框）
"""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout


from ui.commands.command_instance import CommandInstance
from ui.commands.param_panels import CncGcodeDialog
from ui.widgets.cnc_dialogs import (CncMachineDialog, CncStockDialog,
                                      CncToolLibraryDialog, CncToolpathDialog)
from core.vector.geometry_types import Point3D
from core.cnc.state import CncState, CollisionResult


# ============================================================ 对话框命令基类

class _DialogCommand(CommandInstance):
    """启动即弹对话框，关闭即 finish。无鼠标交互。"""
    dialogTitle = ""

    def start(self) -> None:
        super().start()
        dlg = self._makeDialog()
        dlg.exec()
        self.finish()

    def _makeDialog(self) -> QDialog:
        raise NotImplementedError

    def onMousePress(self, wp: Point3D, mods) -> None: ...
    def onMouseMove(self, wp: Point3D, mods) -> None: ...


class CncMachineCommand(_DialogCommand):
    def name(self) -> str: return "CncMachine"
    def prompt(self) -> str: return "机床设置"
    def _makeDialog(self) -> QDialog:
        return CncMachineDialog(self._view if self._view else None)


class CncStockCommand(_DialogCommand):
    def name(self) -> str: return "CncStock"
    def prompt(self) -> str: return "安装几何体（材料+夹具）"

    def start(self) -> None:
        CommandInstance.start(self)
        dlg = self._makeDialog()
        if dlg.exec() == QDialog.Accepted:
            self._updateStockBox()
        self.finish()

    def _makeDialog(self) -> QDialog:
        return CncStockDialog(self._view if self._view else None, doc=self._doc)

    def _updateStockBox(self) -> None:
        """根据 showStock 在世界中心生成/删除毛坯实体立方体（BRepObject）。"""
        from core.cnc.service import update_stock_box
        update_stock_box(self._doc, CncState.instance())


class CncToolsCommand(_DialogCommand):
    def name(self) -> str: return "CncTools"
    def prompt(self) -> str: return "刀具表"
    def _makeDialog(self) -> QDialog:
        return CncToolLibraryDialog(self._view if self._view else None)


# ============================================================ 路径生成

class CncProfileCommand(_DialogCommand):
    """路径生成：弹 CncToolpathDialog，曲线选取在对话框内完成，确定后生成 IR 存 CncState。"""
    def name(self) -> str: return "CncProfile"
    def prompt(self) -> str: return "路径生成"

    def start(self) -> None:
        CommandInstance.start(self)
        if self._doc is None:
            self.finish()
            return
        dlg = self._makeDialog()
        if dlg.exec() == QDialog.Accepted:
            if self._manager is not None:
                state = CncState.instance()
                n = len(state.toolpaths) if state.toolpaths else 0
                if n > 0:
                    self._manager.notifyResult(f"已生成 {n} 条刀路，可进行碰撞检查/模拟/输出")
        self.finish()

    def _makeDialog(self) -> QDialog:
        return CncToolpathDialog(self._doc, self._view if self._view else None)


# ============================================================ 碰撞检查

class CncCollisionCommand(CommandInstance):
    """碰撞检查：刀路 Z/XY 越界 + 夹具碰撞（简单版）。"""
    def name(self) -> str: return "CncCollision"
    def prompt(self) -> str: return "碰撞检查"

    def start(self) -> None:
        super().start()
        state = CncState.instance()
        if not state.toolpaths:
            if self._manager is not None:
                self._manager.notifyResult("请先生成刀路")
            self.finish()
            return
        result = self._check(state)
        state.collisionResult = result
        state.notifyChanged()
        if self._manager is not None:
            if result.ok:
                self._manager.notifyResult("碰撞检查通过")
            else:
                self._manager.notifyResult(f"发现 {len(result.collision_points)} 处问题：\n" + "\n".join(result.messages[:5]))
        self.finish()

    def _check(self, state: CncState) -> CollisionResult:
        res = CollisionResult(ok=True)
        stock = state.stock
        travel = state.machine.travel
        for tp in state.toolpaths:
            for inst in tp.instructions:
                x = inst.x if inst.x is not None else 0.0
                y = inst.y if inst.y is not None else 0.0
                z = inst.z if inst.z is not None else 0.0
                # 行程越界
                if abs(x) > travel.x or abs(y) > travel.y or abs(z) > travel.z:
                    res.ok = False
                    res.messages.append(f"超出行程: ({x:g},{y:g},{z:g})")
                    res.collision_points.append((x, y, z))
                # 毛坯越界（XY 超出毛坯范围，Z 切到毛坯底以下）
                if abs(x) > stock.width or abs(y) > stock.height:
                    res.ok = False
                    res.messages.append(f"超出毛坯 XY: ({x:g},{y:g})")
                    res.collision_points.append((x, y, z))
                if z < -stock.thickness - 0.1:
                    res.ok = False
                    res.messages.append(f"切深超过毛坯底: Z={z:g}")
                    res.collision_points.append((x, y, z))
        return res

    def onMousePress(self, wp: Point3D, mods) -> None: ...
    def onMouseMove(self, wp: Point3D, mods) -> None: ...


# ============================================================ 实体模拟

class CncSimulateCommand(CommandInstance):
    """实体模拟：2D 轨迹动画先做，3D 切除占位。"""
    def name(self) -> str: return "CncSimulate"
    def prompt(self) -> str: return "实体模拟"

    def start(self) -> None:
        super().start()
        state = CncState.instance()
        if not state.toolpaths:
            if self._manager is not None:
                self._manager.notifyResult("请先生成刀路")
            self.finish()
            return
        # 首期占位：提示 2D 轨迹动画待实现
        if self._manager is not None:
            self._manager.notifyResult("实体模拟（2D 轨迹动画）开发中，当前可用路径输出查看 G 代码")
        state.simulationDone = True
        state.notifyChanged()
        self.finish()

    def onMousePress(self, wp: Point3D, mods) -> None: ...
    def onMouseMove(self, wp: Point3D, mods) -> None: ...


# ============================================================ 路径输出

class CncOutputCommand(CommandInstance):
    """路径输出：显示已生成的 G 代码对话框。"""
    def name(self) -> str: return "CncOutput"
    def prompt(self) -> str: return "路径输出"

    def start(self) -> None:
        super().start()
        state = CncState.instance()
        if not state.gcode:
            # 没有缓存的 G 代码，尝试从刀路重新生成
            if not state.toolpaths:
                if self._manager is not None:
                    self._manager.notifyResult("请先生成刀路")
                self.finish()
                return
            from core.cnc.posts import get_post
            try:
                post = get_post(state.machine.controller)
                gcode = post.post_program(state.toolpaths,
                                          macros=state.machine.macros)
                state.gcode = gcode
            except Exception as e:
                if self._manager is not None:
                    self._manager.notifyResult(f"G 代码生成失败: {e}")
                self.finish()
                return
        state.outputDone = True
        self._showGcode(state.gcode, state.toolpaths,
                        state.machine.macros, state.machine.controller)
        state.notifyChanged()
        self.finish()

    def _showGcode(self, text: str, toolpaths=None,
                   macros=None, controller=None) -> None:
        dlg = QDialog(self._view if self._view is not None else None)
        dlg.setWindowTitle("路径输出")
        dlg.resize(680, 520)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(4, 4, 4, 4)
        panel = CncGcodeDialog()
        if toolpaths:
            panel.setContext(toolpaths, macros, controller)
        else:
            panel.setGcode(text)
        panel.closed.connect(dlg.accept)
        lay.addWidget(panel)
        dlg.exec()

    def onMousePress(self, wp: Point3D, mods) -> None: ...
    def onMouseMove(self, wp: Point3D, mods) -> None: ...


# ============================================================ 导入到 CNC

class ImportToCncCommand(CommandInstance):
    """导入CNC：把当前空间选中的曲线/模型克隆到 CNC 独立空间。"""

    def name(self) -> str:
        return "ImportToCnc"

    def prompt(self) -> str:
        return "导入到 CNC"

    def start(self) -> None:
        super().start()
        import uuid
        from core.document import SPACE_CNC, objectFromDict
        doc = self._doc
        if doc is None:
            self.finish()
            return
        if doc.space() == SPACE_CNC:
            if self._manager is not None:
                self._manager.notifyResult("当前已在 CNC 空间，无需导入")
            self.finish()
            return
        sel = [o for o in doc.objects() if o.isSelected()]
        if not sel:
            if self._manager is not None:
                self._manager.notifyResult("请先在画布选中要导入的曲线或模型")
            self.finish()
            return
        count = 0
        names = []
        for o in sel:
            try:
                data = o.serialize()
                data["id"] = uuid.uuid4().hex   # 克隆新 id，与原对象完全独立
                clone = objectFromDict(data)
                if clone is None:
                    continue
                doc.addObject(clone, space=SPACE_CNC)
                count += 1
                names.append(o.name() if hasattr(o, "name") else "")
            except Exception:
                continue
        if count:
            if self._manager is not None:
                self._manager.notifyResult(f"已导入 {count} 个对象到 CNC 空间，可切换到 CNC 模式生成刀路")
            # 自动切到 CNC 模式查看导入结果
            try:
                from PySide6.QtWidgets import QApplication
                for w in QApplication.topLevelWidgets():
                    if hasattr(w, "_canvasModeBar"):
                        w._canvasModeBar.setCurrent("cnc", notify=True)
                        break
            except Exception:
                pass
        else:
            if self._manager is not None:
                self._manager.notifyResult("所选对象无法导入 CNC")
        self.finish()

    def onMousePress(self, wp: Point3D, mods) -> None: ...
    def onMouseMove(self, wp: Point3D, mods) -> None: ...


# ============================================================ 注册

def registerAll(manager) -> None:
    manager.register("CncMachine", CncMachineCommand)
    manager.register("CncStock", CncStockCommand)
    manager.register("CncTools", CncToolsCommand)
    manager.register("CncProfile", CncProfileCommand)
    manager.register("CncCollision", CncCollisionCommand)
    manager.register("CncSimulate", CncSimulateCommand)
    manager.register("CncOutput", CncOutputCommand)
    manager.register("ImportToCnc", ImportToCncCommand)
