"""AI 工具注册表：把软件能力封装为 LLM function-calling / MCP 工具。

设计：
- 面板内对话与外部 MCP 客户端（mcp/cangai_mcp_server.py）**共用同一套工具**；
- 工具实现全部走 `core.api.CangaiAPI` 门面（runScript / commandManifest /
  对象操作），不直接依赖 MainWindow 内部实现；
- `invoke()` 只会从工作线程（MCP 桥/HTTP 线程）被调用：涉及 Qt 对象的工具
  经主线程投递 + 同步等待，保证线程安全。工具注册由宿主在 Qt 主线程完成。
"""
from __future__ import annotations

import base64
import json
import math
import uuid
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 主线程调度（由宿主 MainWindow 注入）
# ---------------------------------------------------------------------------

# 写给大模型的系统提示：面板对话与 MCP 客户端共用
SYSTEM_PROMPT = """你是「Cangai 浮雕」的内置 AI 助手。这是一款浮雕雕刻/CNC 数控加工方向的"
"二维三维 CAD 软件：单位 mm，世界坐标 z=0 为加工床面，浮雕高度沿 +z；"
"画布模式有 2D / 浮雕 / 3D / CNC；对象类型含曲线(CURVE/LINE/CIRCLE/ELLIPSE/ARC/"
"POLYCURVE等)、浮雕网格(SURFACE)、实体(SOLID/BREP)、图片(IMAGE)、标注(DIMENSION)。

你能通过工具直接操作软件，原则：
1. 改文档前先 get_document_info / list_objects 了解现状；做完关键操作可 capture_viewport 检查效果。
2. 简单几何用 add_circle/add_line/add_polyline/add_points；批量或复杂逻辑用 run_script"
"（脚本里 api 是 CangaiAPI 门面：objects/addObject/addPolyline/addCircle/addLine/"
"select/runAction/commandManifest 等，结果赋给 result）。3D 实体建模用 create_primitive"
"（box/cylinder/cone/torus）+ boolean_op（union/cut/common，cut=在 target 上挖掉 tool，"
"如“立方体上挖圆柱洞”=先 create_primitive box 再 create_primitive cylinder 再 boolean_op cut）。
2D→ 3D 转换：闭合曲线/圆/椭圆挤出成实体用 extrude_objects（height 高度、可选 draft 拔模角）。
浮雕高度场类操作（区域浮雕/坡度/图章/平滑）尚未封装为专用工具，需时走 run_script 或提醒用户。
3. 软件界面全部功能都在命令清单里：先 list_commands 查真实命令名再 run_command。"
"kind=action 立即完成；kind=command 会进入画布交互（需要用户在画布上点选），"
"此时告诉用户接下来在画布上点什么，不要重复触发。
4. 图片理解：用户发的图片已附在对话里；分析本地图片文件用 analyze_image。
5. 导出用 export_file（需绝对路径）；撤销恢复用 undo/redo。
6. 删除等破坏性操作前先复述对象清单；出错时把错误原因用中文解释并给出下一步建议。
7. 回复用简体中文、简洁；工具返回的 id 等关键信息要告知用户。
"""

_postToMain: Optional[Callable[[Callable[[], Any]], None]] = None
_inMainThread: Optional[Callable[[], bool]] = None


def setMainThreadInvoker(post: Callable[[Callable[[], Any]], None],
                         isMain: Callable[[], bool]) -> None:
    """宿主注入：post(fn) 投递到 Qt 主线程；isMain() 当前是否主线程。"""
    global _postToMain, _inMainThread
    _postToMain, _inMainThread = post, isMain


def _runOnMain(fn: Callable[[], Any], timeout: float = 30.0) -> Any:
    """把 fn 送到主线程执行并同步取返回值（超时抛 TimeoutError）。"""
    if _inMainThread is not None and _inMainThread():
        return fn()
    if _postToMain is None:
        # 无宿主/无头自动化：没有 Qt 事件循环，直接在当前线程执行
        return fn()
    import threading
    done = threading.Event()
    box: Dict[str, Any] = {}

    def _wrapper() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:      # 含 Qt 异常：原样带回调用线程
            box["error"] = e
        finally:
            done.set()

    _postToMain(_wrapper)
    if not done.wait(timeout):
        raise TimeoutError("主线程执行超时（界面忙或未响应）")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# 工具表
# ---------------------------------------------------------------------------

# name -> {"schema": OpenAI function schema, "handler": fn(args) -> json值}
_TOOLS: Dict[str, dict] = {}


def _register(name: str, description: str,
              parameters: Optional[dict] = None,
              handler: Callable[[dict], Any] = None) -> None:
    _TOOLS[name] = {
        "schema": {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters or
                {"type": "object", "properties": {}},
            },
        },
        "handler": handler,
    }


def toolSchemas() -> List[dict]:
    """全部工具的 OpenAI function-calling schema 列表（供对话面板发给 LLM）。"""
    return [t["schema"] for t in _TOOLS.values()]


def mcpToolDefs() -> List[dict]:
    """MCP `tools/list` 规范格式：[{name, description, inputSchema}]。
    与 toolSchemas() 同源，仅结构不同（MCP 用 inputSchema 而非 function.parameters）。"""
    out = []
    for t in _TOOLS.values():
        fn = t["schema"]["function"]
        out.append({"name": fn["name"],
                    "description": fn["description"],
                    "inputSchema": fn["parameters"]})
    return out


# 工具结果中代表图片的字段（base64）
_IMAGE_B64_KEYS = ("image_png_b64", "image_b64")


def toMcpContent(result: Any) -> List[dict]:
    """把工具返回值转为 MCP `content` 列表：图片字段单独成 image part，
    其余以 JSON text part 返回（让客户端能真正“看到”截图/图片）。"""
    if isinstance(result, dict):
        b64 = None
        for k in _IMAGE_B64_KEYS:
            if result.get(k):
                b64 = result[k]
                break
        if b64:
            mime = result.get("mime") or "image/png"
            rest = {k: v for k, v in result.items()
                    if k not in _IMAGE_B64_KEYS and k != "mime"}
            return [
                {"type": "text",
                 "text": json.dumps(rest, ensure_ascii=False, default=str)},
                {"type": "image", "data": b64, "mimeType": mime},
            ]
    return [{"type": "text",
             "text": json.dumps(result, ensure_ascii=False, default=str)}]


def callToolForMcp(name: str, args: Any) -> dict:
    """执行工具并包装为 MCP `tools/call` 结果（{content, isError}）。
    工具报错以 isError 内容返回（而非协议错误），让大模型能读到失败原因。"""
    try:
        return {"content": toMcpContent(invoke(name, args)), "isError": False}
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps(
            {"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)}],
            "isError": True}


def toolNames() -> List[str]:
    return sorted(_TOOLS)


def invoke(name: str, args: dict | str | None, timeout: float = 60.0) -> Any:
    """执行工具（工作线程安全）：返回可 JSON 序列化结果；失败抛异常。"""
    entry = _TOOLS.get(name)
    if entry is None:
        raise KeyError(f"未知工具: {name}")
    if isinstance(args, str):
        args = json.loads(args) if args.strip() else {}
    return _runOnMain(lambda: entry["handler"](args or {}), timeout=timeout)


# ---------------------------------------------------------------------------
# 结果裁剪（防止超长 JSON 撑爆上下文）
# ---------------------------------------------------------------------------

_MAX_ITEMS = 200          # 对象列表最多返回条数
_MAX_FLOATS = 4           # 浮点有效位


def _rnd(v):
    if isinstance(v, float):
        if math.isinf(v) or math.isnan(v):
            return None
        return round(v, _MAX_FLOATS)
    return v


def _bboxOf(obj) -> Optional[dict]:
    try:
        bb = obj.getBoundingBox()
        if not bb.isValid():
            return None
        return {"x0": _rnd(bb.min.x), "y0": _rnd(bb.min.y), "z0": _rnd(bb.min.z),
                "x1": _rnd(bb.max.x), "y1": _rnd(bb.max.y), "z1": _rnd(bb.max.z)}
    except Exception:
        return None


def _objBrief(obj) -> dict:
    d = {"id": obj.id(), "type": obj.type().name,
         "layer": obj.layerId(), "visible": obj.isVisible(),
         "selected": obj.isSelected()}
    bb = _bboxOf(obj)
    if bb:
        d["bbox"] = bb
    return d


# ---------------------------------------------------------------------------
# 工具实现（全部在主线程执行）
# ---------------------------------------------------------------------------

def _api():
    from core.api import getApiInstance
    return getApiInstance()


def _doc():
    return _api().activeDocument()


def _t_get_document_info(args: dict) -> dict:
    doc = _doc()
    layers = []
    for la in doc.layers():
        layers.append({"id": la.id, "name": la.name,
                       "count": doc.layerObjectCount(la.id),
                       "visible": la.visible})
    return {"title": doc.title(), "path": doc.filePath(),
            "objectCount": len(doc.allObjects()),
            "selectedCount": len(doc.selectedObjects()),
            "activeLayer": doc.activeLayerId(), "layers": layers[:_MAX_ITEMS]}


def _t_list_objects(args: dict) -> dict:
    typeName = args.get("type") or None
    layerId = args.get("layer")
    onlySel = args.get("selected")
    objs = _api().objects(typeName, layerId, onlySel)
    total = len(objs)
    objs = objs[:_MAX_ITEMS]
    return {"total": total, "returned": len(objs),
            "objects": [_objBrief(o) for o in objs]}


def _t_get_object(args: dict) -> dict:
    obj = _api().getObject(args["id"])
    if obj is None:
        raise KeyError(f"对象不存在: {args['id']}")
    out = _objBrief(obj)
    # 类型相关关键参数（属性或同名 getter 都试）
    for attr in ("radius", "text", "strokeWidth"):
        v = getattr(obj, attr, None)
        if v is None:
            continue
        if callable(v):
            try:
                v = v()
            except Exception:
                continue
        if isinstance(v, (int, float, str, bool)):
            out[attr] = _rnd(v) if isinstance(v, float) else v
    for getter in ("getCenter", "center"):
        fn = getattr(obj, getter, None)
        if callable(fn):
            try:
                c = fn()
                out["center"] = [_rnd(c.x), _rnd(c.y), _rnd(c.z)]
                break
            except Exception:
                pass
    try:
        data = obj.toDict()
        out["detail"] = {k: v for k, v in data.items()
                         if k not in ("points", "segments", "heightField",
                                      "field", "knots", "ctrlPoints")
                         and isinstance(v, (int, float, str, bool, type(None)))}
    except Exception:
        pass
    return out


def _t_select_objects(args: dict) -> dict:
    api = _api()
    if args.get("all"):
        objs = api.objects()
        api.select([o.id() for o in objs])
    else:
        api.select(list(args.get("ids", [])), append=bool(args.get("append")))
    return {"selected": len(api.selectedObjects())}


def _t_delete_selected(args: dict) -> dict:
    api = _api()
    ids = [o.id() for o in api.selectedObjects()]
    api.beginEdit()
    try:
        for oid in ids:
            api.removeObject(oid)
    finally:
        api.endEdit()
    return {"deleted": len(ids)}


def _t_add_circle(args: dict) -> dict:
    oid = _api().addCircle(float(args["x"]), float(args["y"]),
                          float(args["radius"]))
    return {"id": oid}


def _t_add_line(args: dict) -> dict:
    oid = _api().addLine(float(args["x1"]), float(args["y1"]),
                         float(args["x2"]), float(args["y2"]))
    return {"id": oid}


def _t_add_polyline(args: dict) -> dict:
    pts = args["points"]
    if len(pts) < 2:
        raise ValueError("points 至少 2 个点")
    oid = _api().addPolyline(pts, closed=bool(args.get("closed")))
    return {"id": oid}


def _t_add_points(args: dict) -> dict:
    api = _api()
    ids = [api.addPoint(float(p[0]), float(p[1])) for p in args["points"]]
    return {"ids": ids}


def _t_list_commands(args: dict) -> dict:
    manifest = _api().commandManifest()
    group = args.get("group")
    if group:
        manifest = [m for m in manifest if m.get("group") == group]
    kw = (args.get("keyword") or "").lower()
    if kw:
        manifest = [m for m in manifest
                    if kw in m["name"].lower() or kw in m["title"].lower()
                    or kw in m["description"].lower()]
    return {"total": len(manifest), "commands": manifest[:_MAX_ITEMS]}


def _t_run_command(args: dict) -> dict:
    """执行动作/交互命令。动作直接执行；交互命令进入画布拾取流程（同用户点按钮）。"""
    api = _api()
    name = args["name"]
    entry = api._actions.get(name)
    if entry is not None:
        api.runAction(name, {})
        return {"executed": name, "kind": "action"}
    if name in api._commands:
        ok = api.executeCommand(name)
        return {"executed": name, "kind": "command", "started": bool(ok)}
    # 宿主内置命令（未在门面注册的同名命令）
    if api.hasHost():
        ok = api.executeCommand(name)
        return {"executed": name, "kind": "host_command", "started": bool(ok)}
    raise KeyError(f"命令未注册: {name}")


def _t_run_script(args: dict) -> dict:
    """执行 Python 脚本（注入 api 门面；把返回值赋给 result）。"""
    res = _api().runScript(args["code"])
    try:
        json.dumps(res)
    except (TypeError, ValueError):
        res = str(res)
    return {"result": res}


def _t_undo(args: dict) -> dict:
    _doc().undo()
    return {"undone": True, "canUndo": _doc().canUndo()}


def _t_redo(args: dict) -> dict:
    _doc().redo()
    return {"redone": True, "canRedo": _doc().canRedo()}


def _t_export(args: dict) -> dict:
    fmt = (args.get("format") or "").lower().lstrip(".")
    path = args.get("path") or ""
    if not path:
        raise ValueError("必须给出导出文件完整路径 path")
    doc = _doc()
    selOnly = bool(args.get("selected_only"))
    targets = doc.selectedObjects() if selOnly else None
    if fmt == "dxf":
        from core.export.dxf_exporter import exportDXF
        n = exportDXF(doc, path, objects=targets)
    elif fmt == "svg":
        from core.export.svg_exporter import exportSVG
        n = exportSVG(doc, path, objects=targets)
    elif fmt == "stl":
        from core.export.stl_exporter import exportSTL
        n = exportSTL(doc, path, surfaces=targets)
    elif fmt == "step":
        from core.export.step_exporter import exportSTEP
        n = exportSTEP(doc, path, solids=targets)
    else:
        raise ValueError(f"不支持的导出格式: {fmt}（dxf/svg/stl/step）")
    return {"path": path, "format": fmt, "count": n}


def _t_place_image(args: dict) -> dict:
    """把图片文件放到文档图片层（矩形范围由 x0,y0,x1,y1 指定）。"""
    import os
    path = args["path"]
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    from core.vector.image_object import ImageObject
    from core.vector.geometry_types import Point3D
    obj = ImageObject.fromFile(path)
    x0, y0 = float(args.get("x0", 0)), float(args.get("y0", 0))
    x1, y1 = float(args.get("x1", 100)), float(args.get("y1", 100))
    w, h = abs(x1 - x0), abs(y1 - y0)
    obj.setGeometry(Point3D(x0 + w / 2, y0 + h / 2, 0.0),
                    max(w, 1e-6), max(h, 1e-6))
    oid = _api().addObject(obj)
    return {"id": oid}


def _host():
    api = _api()
    return api._host if api.hasHost() else None


def _t_capture_viewport(args: dict) -> dict:
    """截取当前视口画面，返回 base64 PNG（供 AI“看”用户所见）。"""
    host = _host()
    if host is None:
        raise RuntimeError("截图需要界面宿主")
    view = getattr(host, "_viewport3D", None) \
        if getattr(host, "_active3D", False) else getattr(host, "_view", None)
    if view is None:
        raise RuntimeError("找不到当前活动视口")
    pix = view.grab()
    maxW = int(args.get("max_width", 960))
    if pix.width() > maxW:
        pix = pix.scaledToWidth(maxW)
    import io
    buf = io.BytesIO()
    pix.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"image_png_b64": b64, "width": pix.width(), "height": pix.height()}


def _t_analyze_image(args: dict) -> dict:
    """读取图片文件并转 base64，配合 prompt 交给多模态模型分析。

    面板会跟踪本次调用，把图片附到下一轮对话，让模型真正“看图”。"""
    import os
    path = args["path"]
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("图片过大（>8MB）")
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    return {"path": path, "mime": f"image/{ext}",
            "image_b64": base64.b64encode(raw).decode("ascii"),
            "note": args.get("prompt", "请分析这张图片")}


def _t_set_layer(args: dict) -> dict:
    doc = _doc()
    lid = int(args["layer"])
    doc.setLayerActive(lid)
    return {"activeLayer": doc.activeLayerId()}


def _t_create_primitive(args: dict) -> dict:
    """创建 3D 标准体（build123d 内核）：box/cylinder/cone/torus。"""
    from core.vector.brep_shape import BrepShape, bdKernelReady, bdKernelError
    from core.vector.brep_object import BRepObject
    if not bdKernelReady():
        err = bdKernelError()
        raise RuntimeError("3D 几何内核(build123d)尚未就绪，请稍后重试"
                           + (f"：{err}" if err else ""))
    kind = args["kind"]
    p = {k: args[k] for k in
         ("l", "w", "h", "d", "d1", "d2", "dmaj", "dmin", "draft", "axis", "rot")
         if k in args}
    p["cx"] = float(args.get("cx", 0.0))
    p["cy"] = float(args.get("cy", 0.0))
    p["cz"] = float(args.get("cz", 0.0))
    p["align"] = args.get("align", "bottom")
    shape = BrepShape.fromPrimitive(kind, p)
    obj = BRepObject()
    obj.setShape(shape)
    oid = _api().addObject(obj)
    return {"id": oid, "kind": kind, "bbox": _bboxOf(obj)}


def _t_boolean_op(args: dict) -> dict:
    """两 3D 实体布尔：union(A∪B) / cut(A−B，在 A 上挖 B) / common(A∩B)。
    结果替换 target（tool 被消耗）。复用软件 brep_ops.booleanOp（OCCT 内核）。"""
    from core.vector.brep_shape import BrepShape, bdKernelReady
    from core.vector.brep_object import BRepObject
    from core.vector.brep_ops import booleanOp
    if not bdKernelReady():
        raise RuntimeError("3D 几何内核(build123d)尚未就绪，请稍候重试")
    api = _api()
    a = api.getObject(args["target_id"])
    b = api.getObject(args["tool_id"])
    if a is None or b is None:
        raise KeyError("实体 id 不存在")
    if a.type().name != "BREP" or b.type().name != "BREP":
        raise ValueError("布尔运算需要两个 3D 实体（BREP），先用 create_primitive 创建")
    op = args.get("op", "cut")
    if op not in ("union", "cut", "common"):
        raise ValueError("op 必须为 union/cut/common")
    res = booleanOp(a.shape().part(), b.shape().part(), op)
    obj = BRepObject()
    obj.setShape(BrepShape.fromPart(res))
    removed = [a.id(), b.id()]
    api.beginEdit()
    try:
        for oid in removed:
            api.removeObject(oid)
        oid = api.addObject(obj)
    finally:
        api.endEdit()
    return {"id": oid, "op": op, "consumed": removed, "bbox": _bboxOf(obj)}


# ---------------------------------------------------------------------------
# 2D 编辑工具（直接调 core/vector 几何内核，全自动、无需画布交互）
# ---------------------------------------------------------------------------

def _targets(args: dict):
    """解析 ids 或当前选中 → 对象列表。"""
    api = _api()
    ids = args.get("ids")
    if ids:
        return [o for o in (api.getObject(i) for i in ids) if o is not None]
    return api.selectedObjects()


def _samplePts(obj):
    """对象 → 世界坐标点列（供偏移/布尔等点运算）。"""
    import math
    from core.vector.geometry_types import Point3D
    for attr in ("points", "getSampledPoints", "samplePoints"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                pts = fn()
            except Exception:
                pts = None
            if pts and len(pts) >= 2:
                return [p if hasattr(p, "x") else Point3D(*p) for p in pts]
    if obj.type().name == "CIRCLE":
        c = obj.center(); r = obj.radius()
        return [Point3D(c.x + r * math.cos(2 * math.pi * i / 72),
                        c.y + r * math.sin(2 * math.pi * i / 72), c.z)
                for i in range(72)]
    return []


def _isClosed(obj) -> bool:
    fn = getattr(obj, "isClosed", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return bool(getattr(obj, "_isClosed", False))


def _t_transform_objects(args: dict) -> dict:
    """批量变换：move(dx,dy,dz) / rotate(angle度,cx,cy) / scale(sx,sy,cx,cy)。"""
    import math
    from core.vector.geometry_types import Point3D
    from core.vector.transform_geometry import scaleObjectAround
    api = _api(); doc = api.activeDocument()
    objs = _targets(args)
    if not objs:
        raise ValueError("没有目标对象：传 ids 或先在画布选中")
    action = args["action"]
    cx = float(args.get("cx", 0.0)); cy = float(args.get("cy", 0.0))
    api.beginEdit()
    try:
        for o in objs:
            if action == "move":
                o.translate(float(args.get("dx", 0)), float(args.get("dy", 0)),
                            float(args.get("dz", 0)))
            elif action == "rotate":
                rad = math.radians(float(args.get("angle", 0)))
                o.translate(-cx, -cy, 0); o.rotate(rad, 0, 0, 1); o.translate(cx, cy, 0)
            elif action == "scale":
                sx = float(args.get("sx", 1)); sy = float(args.get("sy", sx))
                scaleObjectAround(o, sx, sy, float(args.get("sz", 1)),
                                  Point3D(cx, cy, 0), doc)
            else:
                raise ValueError("action 必须 move/rotate/scale")
            try:
                doc.objectChanged.emit(o.id())
            except Exception:
                pass
    finally:
        api.endEdit()
    return {"action": action, "transformed": [o.id() for o in objs]}


def _t_mirror_objects(args: dict) -> dict:
    from core.vector.mirror_geometry import mirrorObject
    from core.vector.geometry_types import Point3D
    api = _api(); objs = _targets(args)
    if not objs:
        raise ValueError("没有目标对象")
    a = args["axis_p1"]; b = args["axis_p2"]
    p1 = Point3D(float(a[0]), float(a[1]), 0.0)
    p2 = Point3D(float(b[0]), float(b[1]), 0.0)
    api.beginEdit(); ids = []
    try:
        for o in objs:
            m = mirrorObject(o, p1, p2)
            if m is not None:
                ids.append(api.addObject(m))
    finally:
        api.endEdit()
    return {"mirrored": ids, "count": len(ids)}


def _t_array_rect(args: dict) -> dict:
    from core.vector.array_generator import rectangularArray
    api = _api(); doc = api.activeDocument(); objs = _targets(args)
    if not objs:
        raise ValueError("没有目标对象")
    xc = int(args["x_count"]); yc = int(args["y_count"])
    if xc < 1 or yc < 1 or xc * yc > 2000:
        raise ValueError("阵列数量非法或过大（x_count*y_count 需 ≤2000）")
    api.beginEdit()
    try:
        rectangularArray(doc, objs, xc, yc,
                         float(args.get("x_gap", 0)), float(args.get("y_gap", 0)))
    finally:
        api.endEdit()
    return {"kind": "rect", "x_count": xc, "y_count": yc,
            "total_objects": len(doc.allObjects())}


def _t_array_circular(args: dict) -> dict:
    from core.vector.array_generator import circularArray
    from core.vector.geometry_types import Point3D
    api = _api(); doc = api.activeDocument(); objs = _targets(args)
    if not objs:
        raise ValueError("没有目标对象")
    count = int(args["count"])
    if count < 2 or count > 2000:
        raise ValueError("count 需 2..2000")
    c = args.get("center", [0, 0])
    center = Point3D(float(c[0]), float(c[1]), 0.0)
    api.beginEdit()
    try:
        circularArray(doc, objs, center, count,
                      float(args.get("radius", 0)),
                      float(args.get("start_angle", 0)),
                      float(args.get("angle_range", 360)),
                      bool(args.get("rotate_objects", True)))
    finally:
        api.endEdit()
    return {"kind": "circular", "count": count,
            "total_objects": len(doc.allObjects())}


def _t_offset_objects(args: dict) -> dict:
    """曲线偏移：闭合曲线外/内偏移 dist；开放曲线偏移成闭合包裹带。"""
    from core.vector.outline_geometry import offsetClosedOutward, offsetOpenAsBand
    api = _api(); objs = _targets(args)
    if not objs:
        raise ValueError("没有目标对象")
    dist = float(args["distance"])
    api.beginEdit(); ids = []
    try:
        for o in objs:
            pts = _samplePts(o)
            if len(pts) < 2:
                continue
            res = (offsetClosedOutward(pts, dist) if _isClosed(o)
                   else offsetOpenAsBand(pts, dist))
            if res:
                ids.append(api.addPolyline([(p.x, p.y, p.z) for p in res],
                                           closed=True))
    finally:
        api.endEdit()
    return {"offset": ids, "count": len(ids), "distance": dist}


def _t_boolean_union_curves(args: dict) -> dict:
    """多条闭合曲线布尔并集（去内孔取外环），结果为新闭合曲线。"""
    from core.vector.outline_geometry import booleanUnion, outerRings
    api = _api(); objs = _targets(args)
    paths = [_samplePts(o) for o in objs]
    paths = [p for p in paths if len(p) >= 3]
    if len(paths) < 2:
        raise ValueError("布尔并集需要至少 2 条闭合曲线")
    z = paths[0][0].z
    rings = outerRings(booleanUnion(paths, z))
    api.beginEdit(); ids = []
    try:
        for ring in rings:
            if len(ring) >= 3:
                ids.append(api.addPolyline([(p.x, p.y, p.z) for p in ring],
                                           closed=True))
    finally:
        api.endEdit()
    return {"united": ids, "rings": len(ids)}


def _t_extend_curve(args: dict) -> dict:
    from core.vector.extend_geometry import extendObject
    api = _api(); doc = api.activeDocument()
    o = api.getObject(args["id"])
    if o is None:
        raise KeyError("对象不存在")
    api.beginEdit()
    try:
        new_obj, replaced = extendObject(
            o, bool(args.get("at_head", False)), float(args["distance"]))
        if new_obj is None:
            raise ValueError("延伸失败（对象不适合延伸或距离≤0）")
        if replaced:
            doc.replaceObject(o.id(), new_obj)
        try:
            doc.objectChanged.emit(o.id())
        except Exception:
            pass
        out_id = o.id()
    finally:
        api.endEdit()
    return {"extended": out_id, "replaced": replaced,
            "distance": args["distance"]}


def _t_extrude_objects(args: dict) -> dict:
    """把闭合曲线挤出成 3D 实体（build123d 内核）。"""
    from core.vector.brep_shape import BrepShape, bdKernelReady, bdKernelError
    from core.vector.brep_object import BRepObject
    if not bdKernelReady():
        err = bdKernelError()
        raise RuntimeError("3D 几何内核(build123d)尚未就绪，请稍后重试"
                           + (f"：{err}" if err else ""))
    api = _api()
    ids = args.get("ids")
    objs = [api.getObject(i) for i in ids] if ids else api.selectedObjects()
    objs = [o for o in objs if o is not None]
    if not objs:
        raise ValueError("没有目标闭合曲线：传 ids 或先选中")
    height = float(args["height"])
    draft = float(args.get("draft", 0.0))
    if height <= 0:
        raise ValueError("height 需 >0")
    api.beginEdit(); out = []
    try:
        for o in objs:
            pts = _samplePts(o)
            if len(pts) < 3:
                continue
            shape = BrepShape.fromExtrude(pts, height, draft, None, "polygon")
            obj = BRepObject()
            obj.setShape(shape)
            out.append(api.addObject(obj))
    finally:
        api.endEdit()
    return {"extruded": out, "count": len(out), "height": height}


# ---------------------------------------------------------------------------
# 注册（schema 描述写给大模型看，务必准确）
# ---------------------------------------------------------------------------

_O = {"type": "object", "properties": {}, "additionalProperties": False}


def _objp(props: dict) -> dict:
    required = [k for k, v in props.items() if v.get("_required")]
    clean = {k: {kk: vv for kk, vv in v.items() if kk != "_required"}
             for k, v in props.items()}
    return {"type": "object", "properties": clean,
            "required": required, "additionalProperties": False}


def _registerAll() -> None:
    _register("get_document_info",
              "获取当前文档概况：标题/路径/对象总数/选中数/图层列表。回答任何"
              "“现在有什么/几个”类问题前先调用。", None, _t_get_document_info)
    _register("list_objects",
              "列出文档对象（id/类型/图层/包围盒/是否选中）。可按类型名过滤，"
              "如 CURVE/LINE/CIRCLE/ELLIPSE/ARC/POLYCURVE/SURFACE/SOLID/BREP/IMAGE/DIMENSION。",
              _objp({"type": {"type": "string", "description": "类型名过滤，可省"},
                     "layer": {"type": "integer", "description": "图层 id 过滤，可省"},
                     "selected": {"type": "boolean", "description": "true=只列选中"}}),
              _t_list_objects)
    _register("get_object", "按 id 获取单个对象的详细参数（圆心半径/包围盒等）。",
              _objp({"id": {"type": "string", "_required": True}}), _t_get_object)
    _register("select_objects", "按 id 列表设置选中对象；all=true 全选；append=true 追加选。",
              _objp({"ids": {"type": "array", "items": {"type": "string"}},
                     "all": {"type": "boolean"}, "append": {"type": "boolean"}}),
              _t_select_objects)
    _register("delete_selected", "删除当前选中的全部对象（可撤销）。", None,
              _t_delete_selected)
    _register("set_active_layer", "切换活动图层（后续新建对象归属该图层族）。",
              _objp({"layer": {"type": "integer", "_required": True}}), _t_set_layer)
    _register("add_circle", "创建圆（世界坐标 mm）。",
              _objp({"x": {"type": "number", "_required": True},
                     "y": {"type": "number", "_required": True},
                     "radius": {"type": "number", "_required": True}}), _t_add_circle)
    _register("add_line", "创建直线段（世界坐标 mm）。",
              _objp({"x1": {"type": "number", "_required": True},
                     "y1": {"type": "number", "_required": True},
                     "x2": {"type": "number", "_required": True},
                     "y2": {"type": "number", "_required": True}}), _t_add_line)
    _register("add_polyline", "由点列创建折线/多段线曲线对象；closed=true 闭合。",
              _objp({"points": {"type": "array", "_required": True,
                               "items": {"type": "array", "items": {"type": "number"},
                                         "minItems": 2, "maxItems": 3},
                               "description": "[[x,y],...] 或 [[x,y,z],...]"},
                     "closed": {"type": "boolean"}}), _t_add_polyline)
    _register("add_points", "批量创建点对象。",
              _objp({"points": {"type": "array", "_required": True,
                               "items": {"type": "array",
                                         "items": {"type": "number"}}}}),
              _t_add_points)
    _register("list_commands",
              "查询软件全部可执行命令清单（name/kind/title/description/group），"
              "即 UI 工具栏上的所有功能。执行命令前先用它确认真实命令名。",
              _objp({"group": {"type": "string", "description": "按分组过滤，可省"},
                     "keyword": {"type": "string", "description": "关键词模糊过滤，可省"}}),
              _t_list_commands)
    _register("run_command",
              "按名称执行命令。kind=action 的瞬时命令直接完成；kind=command 的"
              "交互命令会进入画布拾取流程（用户在画布上点选完成，如同点击工具按钮）。",
              _objp({"name": {"type": "string", "_required": True}}), _t_run_command)
    _register("run_script",
              "执行 Python 脚本（最高级工具）。命名空间已注入 api（CangaiAPI 门面："
              "addObject/objects/addPolyline/addCircle/runAction/commandManifest/...），"
              "把要返回的结果赋给变量 result。适合批量几何生成、复杂修改。",
              _objp({"code": {"type": "string", "_required": True}}), _t_run_script)
    _register("undo", "撤销上一步操作。", None, _t_undo)
    _register("redo", "重做。", None, _t_redo)
    _register("export_file",
              "导出文件。format: dxf/svg（曲线）、stl（浮雕网格/实体）、step（B-Rep 实体）。"
              "path 为绝对路径；selected_only=true 只导出选中对象。",
              _objp({"format": {"type": "string", "_required": True,
                               "enum": ["dxf", "svg", "stl", "step"]},
                     "path": {"type": "string", "_required": True},
                     "selected_only": {"type": "boolean"}}), _t_export)
    _register("place_image",
              "把本地图片文件放置到文档图片层，占据 (x0,y0)-(x1,y1) 矩形区域。",
              _objp({"path": {"type": "string", "_required": True},
                     "x0": {"type": "number"}, "y0": {"type": "number"},
                     "x1": {"type": "number"}, "y1": {"type": "number"}}),
              _t_place_image)
    _register("capture_viewport",
              "截取当前视口画面（PNG base64）。用于查看用户所见布局再决定下一步操作。",
              _objp({"max_width": {"type": "integer", "description": "最长边像素，默认960"}}),
              _t_capture_viewport)
    _register("analyze_image",
              "读取本地图片文件内容用于分析：调用后系统会把该图片附到对话，"
              "配合 prompt 你能真正看到图片（尺寸/内容/构图）。",
              _objp({"path": {"type": "string", "_required": True},
                     "prompt": {"type": "string", "description": "想从图里了解什么"}}),
              _t_analyze_image)
    _register("create_primitive",
              "创建 3D 实体标准体（build123d/OCCT 内核，单位 mm）。kind=box 用 l/w/h；"
              "cylinder 用 d/h；cone 用 d1(底径)/d2(顶径,0=尖)/h；torus 用 dmaj(环直径)/dmin(管径)/axis。"
              "cx/cy/cz 定位（默认底面在 cz，align=center 则中心对齐）。返回实体 id。",
              _objp({"kind": {"type": "string", "_required": True,
                             "enum": ["box", "cylinder", "cone", "torus"]},
                     "l": {"type": "number"}, "w": {"type": "number"},
                     "h": {"type": "number"}, "d": {"type": "number"},
                     "d1": {"type": "number"}, "d2": {"type": "number"},
                     "dmaj": {"type": "number"}, "dmin": {"type": "number"},
                     "axis": {"type": "string", "enum": ["z", "x", "y"]},
                     "draft": {"type": "number"}, "rot": {"type": "number"},
                     "cx": {"type": "number"}, "cy": {"type": "number"},
                     "cz": {"type": "number"},
                     "align": {"type": "string", "enum": ["bottom", "center"]}}),
              _t_create_primitive)
    _register("boolean_op",
              "两个 3D 实体布尔运算（较重几何，需内核就绪）。op=cut 在 target 上挖掉 tool"
              "（如立方体减圆柱=打孔），union 合并，common 取交。结果替换 target_id 实体、"
              "tool_id 被消耗，返回新实体 id。",
              _objp({"target_id": {"type": "string", "_required": True},
                     "tool_id": {"type": "string", "_required": True},
                     "op": {"type": "string", "_required": True,
                           "enum": ["union", "cut", "common"]}}),
              _t_boolean_op)

    _register("transform_objects",
              "批量变换对象（就地修改，可撤销）。action=move 用 dx/dy/dz；"
              "rotate 用 angle(度)/cx/cy（绕该中心）；scale 用 sx/sy(可选 sz)/cx/cy（绕该中心，"
              "非等比缩圆会自动变椭圆）。目标为 ids 或当前选中。单位 mm。",
              _objp({"action": {"type": "string", "_required": True,
                               "enum": ["move", "rotate", "scale"]},
                     "ids": {"type": "array", "items": {"type": "string"}},
                     "dx": {"type": "number"}, "dy": {"type": "number"},
                     "dz": {"type": "number"}, "angle": {"type": "number"},
                     "sx": {"type": "number"}, "sy": {"type": "number"},
                     "sz": {"type": "number"}, "cx": {"type": "number"},
                     "cy": {"type": "number"}}),
              _t_transform_objects)
    _register("mirror_objects",
              "镜像对象：生成关于轴线 axis_p1-axis_p2（各为 [x,y]）的镜像副本并加入文档。"
              "目标为 ids 或选中。",
              _objp({"axis_p1": {"type": "array", "items": {"type": "number"}, "_required": True},
                     "axis_p2": {"type": "array", "items": {"type": "number"}, "_required": True},
                     "ids": {"type": "array", "items": {"type": "string"}}}),
              _t_mirror_objects)
    _register("array_rectangular",
              "矩形阵列：以包围盒尺寸+间隙为步距复制对象。x_count/y_count 个数，"
              "x_gap/y_gap 间隙(mm)。目标为 ids 或选中。",
              _objp({"x_count": {"type": "integer", "_required": True},
                     "y_count": {"type": "integer", "_required": True},
                     "x_gap": {"type": "number"}, "y_gap": {"type": "number"},
                     "ids": {"type": "array", "items": {"type": "string"}}}),
              _t_array_rect)
    _register("array_circular",
              "圆形阵列：绕 center([x,y]) 沿圆周分布 count 个副本。radius≤0 时自动取"
              "对象到中心距离；start_angle/angle_range 度数；rotate_objects 副本是否随圆周旋转。",
              _objp({"count": {"type": "integer", "_required": True},
                     "center": {"type": "array", "items": {"type": "number"}},
                     "radius": {"type": "number"}, "start_angle": {"type": "number"},
                     "angle_range": {"type": "number"},
                     "rotate_objects": {"type": "boolean"},
                     "ids": {"type": "array", "items": {"type": "string"}}}),
              _t_array_circular)
    _register("offset_objects",
              "曲线偏移（生成新曲线）：闭合曲线沿法向偏移 distance（正=外偏、负=内偏），"
              "开放曲线偏移成闭合包裹带。目标为 ids 或选中。单位 mm。",
              _objp({"distance": {"type": "number", "_required": True},
                     "ids": {"type": "array", "items": {"type": "string"}}}),
              _t_offset_objects)
    _register("boolean_union_curves",
              "多条闭合曲线布尔并集（对应 Rhino CreateBooleanUnion）：合并重叠区域、"
              "去除内孔，结果为新闭合曲线。目标为 ids 或选中（需 ≥2 条）。",
              _objp({"ids": {"type": "array", "items": {"type": "string"}}}),
              _t_boolean_union_curves)
    _register("extend_curve",
              "延伸开放曲线指定端点 distance 距离（沿端部切线）。at_head=true 延首端，"
              "否则延尾端。作用于单个对象 id。",
              _objp({"id": {"type": "string", "_required": True},
                     "distance": {"type": "number", "_required": True},
                     "at_head": {"type": "boolean"}}),
              _t_extend_curve)
    _register("extrude_objects",
              "把闭合曲线/圆/椭圆挤出成 3D 实体（build123d 内核，2D→3D）。height 挤出高度(mm)，"
              "draft 拔模角(度，可选)。目标 ids 或选中（需闭合轮廓）。返回新实体 id 列表。",
              _objp({"height": {"type": "number", "_required": True},
                     "draft": {"type": "number"},
                     "ids": {"type": "array", "items": {"type": "string"}}}),
              _t_extrude_objects)


_registerAll()
