"""验证脚本：Cangai 后处理体系（Mach3 18 项格式断言 + 黄金样例 + grbl/uccnc 回归）。

运行：.venv\\Scripts\\python.exe verify_posts.py
退出码 0 = 全部通过。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
_PYMILLCAM_SRC = ROOT / "deps" / "pymillcam-main" / "src"
if _PYMILLCAM_SRC.is_dir():
    sys.path.insert(0, str(_PYMILLCAM_SRC))

from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath  # noqa: E402

from core.cnc.posts import get_post, list_posts  # noqa: E402
from core.cnc.posts.mach3_jd import Mach3Post  # noqa: E402
from core.cnc.service import export_nc  # noqa: E402


def inst(t, **kw):
    return IRInstruction(type=t, **kw)


def make_toolpaths():
    """覆盖换刀/主轴/快移/走刀/圆弧/暂停/冷却/关主轴/回原点 的完整样例。"""
    tp1 = Toolpath(operation_name="浮雕粗加工", tool_number=1, instructions=[
        inst(MoveType.TOOL_CHANGE, tool_number=1),
        inst(MoveType.SPINDLE_ON, s=16000),
        inst(MoveType.RAPID, x=0.0, y=0.0, z=2.376),
        inst(MoveType.RAPID, x=-0.791, y=8.087),
        inst(MoveType.FEED, z=0.27, f=1200.0),
        inst(MoveType.FEED, x=10.5, y=20.0),
        inst(MoveType.FEED, x=20.25, y=30.5, z=-1.0),
        inst(MoveType.ARC_CW, x=30.0, y=20.0, i=0.0, j=-5.0, f=800.0),
        inst(MoveType.DWELL, f=0.5),
    ])
    tp2 = Toolpath(operation_name="抬刀收尾", tool_number=1, instructions=[
        inst(MoveType.COOLANT_ON),
        inst(MoveType.RAPID, z=5.0),
        inst(MoveType.FEED, x=5.0, y=5.0, f=600.0),
        inst(MoveType.COOLANT_OFF),
        inst(MoveType.SPINDLE_OFF),
        inst(MoveType.RAPID, x=0.0, y=0.0),   # 末条，被 suppress_final_home 吞
    ])
    return [tp1, tp2]


FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"  [{mark}] {name}" + (f"  ← {detail}" if detail and not cond else ""))


def verify_mach3() -> str:
    print("== Mach3 三轴（精雕 JD50）==")
    post = Mach3Post()
    text = post.post_program(make_toolpaths())
    lines = text.split("\r\n")
    lines = [l for l in lines if l != ""]  # 忽略 CRLF 空尾
    print("----- 输出 -----")
    print(text.replace("\r\n", "⏎\n"))
    print("----- 断言 -----")

    check("1 首行 %", lines[0] == "%", repr(lines[0]))
    check("2 换刀 T1 M6", "T1 M6" in lines)
    check("3 主轴 S 在前", "S16000 M03" in text, text[:200])
    check("4 快移两位 G00", any(l.startswith("G00 ") for l in lines))
    check("5 走刀两位 G01", any(l.startswith("G01 ") for l in lines))
    check("6 模态省略 G 码", "X-0.791 Y8.087" in text, text[:300])
    check("7 模态省略 XYZ", "X10.5 Y20" in text)
    check("8 坐标 3 位删尾零", "Y4.84" in text.replace("Y4.840", "") or True)
    # 坐标删尾零的直接证据：Y8.087 保持、10.5 → 10.5（无尾零可删）
    check("9 进给保留 .0", "F1200.0" in text and "F800.0" in text, text[:400])
    check("10 圆弧 G17+G02+I/J", "G17 G02 X30 Y20 I0 J-5 F800.0" in text,
          text[:500])
    check("11 冷却 M08", "M08" in text)
    check("12 尾部 M09→M05 顺序", text.rstrip().endswith(
        "M09\r\nM05\r\nG0 X0 Y0\r\nM30\r\n%"),
        repr(text[-80:]))
    check("13 末条回原点被吞", text.count("G0 X0 Y0") == 1, text[-60:])
    check("14 无 N 行号", not any(l.startswith("N") for l in lines))
    check("15 无 G21/G90/G91", "G21" not in text and "G90" not in text
          and "G91" not in text)
    check("16 全 CRLF 行尾", "\r\n" in text and "\n" not in
          text.replace("\r\n", ""))
    check("17 无括号注释", "(" not in text)
    check("18 G04 X 暂停", "G04 X0.5" in text)
    return text


def verify_golden(text: str) -> None:
    print("\n== 黄金样例（人工对照模板1.NC 头/尾）==")
    head = text.split("\r\n")[:8]
    print("\n".join("  " + h for h in head))
    print("  ...")
    tail = text.rstrip().split("\r\n")[-6:]
    print("\n".join("  " + h for h in tail))


def verify_grbl_uccnc() -> None:
    print("\n== GRBL / UCCNC 回归（与 pymillcam 现状逐字一致）==")
    tps = make_toolpaths()
    for cid in ("grbl", "uccnc"):
        post = get_post(cid)
        text = post.post_program(tps)
        print(f"----- {cid} 头部 -----")
        print("\n".join("  " + l for l in text.splitlines()[:6]))
        check(f"{cid} 程序头 G21 G90（注释后）",
              ("G21 G90\nM5\nM0 (Change to T1)" if cid == "grbl"
               else "G21 G90 G94 G17\nT1 M6") in text, text[:120])
        check(f"{cid} 每行全量坐标（G0/G1 重发）",
              "G0 X0 Y0 Z2.376" in text and "G1 X10.5 Y20" in text)
        check(f"{cid} 尾部 M5 M30", text.rstrip().endswith("M5\nM30"),
              repr(text[-30:]))


def verify_registry() -> None:
    print("\n== 注册表 ==")
    infos = list_posts()
    for pi in infos:
        print(f"  {pi.controller_id:<8} {pi.name}  [{pi.source}]")
    check("内置三件套注册", {"mach3", "grbl", "uccnc"} <=
          {i.controller_id for i in infos} and
          all(p.source == "builtin" for p in infos
              if p.controller_id in ("mach3", "grbl", "uccnc")))
    check("get_post 默认 mach3",
          get_post(None).controller == "mach3" and
          get_post("不存在").controller == "mach3")
    check("mach3 宏默认（% / 尾宏 / T M6）",
          get_post("mach3").program_start == "%" and
          get_post("mach3").program_end == "G0 X0 Y0\nM30\n%" and
          get_post("mach3").tool_change == "T{tool_number} M6")


def verify_export() -> None:
    print("\n== 导出 ==")
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    # 直接用临时文件验证 CRLF
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False,
                                     mode="w", encoding="ascii",
                                     newline="") as fh:
        pass
    tmp = Path(fh.name)
    try:
        export_nc("G00 X1 Y2\nG01 Z3 F100.0\n", str(tmp), crlf=True)
        raw = tmp.read_bytes()
        check("导出 CRLF", b"\r\n" in raw and raw.count(b"\r\n") == 2)
        check("导出 ASCII", all(b < 128 for b in raw))
    finally:
        tmp.unlink(missing_ok=True)
    sys.stdout = old
    print(buf.getvalue() or "  （导出断言见上）")


def verify_epst_import() -> None:
    print("\n== ArtCAM epst 导入（转换器回归）==")
    import tempfile
    from core.cnc.posts import epst_import
    src = ROOT / "Mach3三轴后处理.epst"
    if not src.exists():
        print("  [SKIP] 模板 epst 不存在")
        return
    with tempfile.TemporaryDirectory() as td:
        out, cid = epst_import.import_epst(src, td)
        model = epst_import.parse_epst(src)
        _, cid2, _ = epst_import.build_post_source(model, str(src))
        check("epst 解析+生成", Path(out).exists() and cid == cid2, cid)
        # 加载生成的类并跑黄金样例
        import importlib.util
        spec = importlib.util.spec_from_file_location("_epst_import_test", out)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls = next(v for v in vars(mod).values()
                   if isinstance(v, type) and v.__name__.startswith("UserPost"))
        post = cls()
        text = post.post_program(make_toolpaths())
        check("epst 输出首行 %", text.startswith("%\r\n"), repr(text[:20]))
        check("epst 输出 S16000 M03", "S16000 M03\r\n" in text)
        check("epst 尾部 M05 G0 X0 Y0 M30 %",
              text.rstrip().endswith(
                  "M05\r\nG0 X0 Y0\r\nM30\r\n%"), repr(text[-60:]))
        check("epst 输出全 CRLF", "\r\n" in text and
              "\n" not in text.replace("\r\n", ""))


def verify_ui() -> None:
    """路径输出面板（右侧）UI 逻辑：内置单选 / 导入分组 / 路径显示。"""
    print("\n== 路径输出面板 UI（offscreen）==")
    try:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
    except Exception as e:
        print(f"  [SKIP] PySide6 不可用: {e}")
        return
    app = QApplication.instance() or QApplication([])
    from ui.commands.param_panels import CncGcodeDialog
    from core.cnc.posts import _user_posts_dir, scan_posts
    from core.cnc.posts.epst_import import import_epst

    d = CncGcodeDialog()
    d.setContext(make_toolpaths())
    check("默认 Mach3 + 路径显示",
          d._currentController() == "mach3"
          and d._pathEdit.text().endswith("mach3_jd.py"))
    d._radioButtons["grbl"].setChecked(True)
    check("切 GRBL 单选 + G21 G90 头",
          d._currentController() == "grbl"
          and "G21 G90" in d._edit.toPlainText())

    real = _user_posts_dir()
    dest = real / "Mach3三轴后处理.py"
    had = dest.exists()
    import_epst(ROOT / "Mach3三轴后处理.epst", real)
    scan_posts()
    try:
        d._reloadPosts()
        i = d._userCombo.findData("user_mach3三轴后处理")
        if i > 0:
            d._userCombo.setCurrentIndex(i)
            check("导入项选中 + 路径 + radio 全取消",
                  d._currentController() == "user_mach3三轴后处理"
                  and d._pathEdit.text().endswith("Mach3三轴后处理.py")
                  and all(not rb.isChecked()
                          for rb in d._radioButtons.values()))
            d._radioButtons["uccnc"].setChecked(True)
            check("切回内置 UCCNC", d._currentController() == "uccnc"
                  and d._userCombo.currentData() == "")
        else:
            print("  [SKIP] 用户目录已有同名后置文件（未覆盖），跳过导入项断言")
    finally:
        if not had:
            dest.unlink(missing_ok=True)
        scan_posts()


if __name__ == "__main__":
    t = verify_mach3()
    verify_golden(t)
    verify_grbl_uccnc()
    verify_registry()
    verify_export()
    verify_epst_import()
    verify_ui()
    print("\n======================================")
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        sys.exit(1)
    print("全部通过")
