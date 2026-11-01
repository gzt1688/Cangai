"""AI 线稿模型打包：把权重做成根目录单文件 ai_models.zip（不随安装包分发）。

用法：
    .venv\\Scripts\\python.exe pack_models.py            生成/更新 ai_models.zip
    .venv\\Scripts\\python.exe pack_models.py -l         列出 zip 内容
    .venv\\Scripts\\python.exe pack_models.py --stored   不压缩（最快，体积≈原文件）

为什么只打三个文件：发布真正需要的只有 A2S.onnx（217MB）、mobile_sam.pt（40MB）、
MTEED.onnx（242KB）；netG.pth / sk_model*.pth / MTEED.pth 是训练与导 ONNX 用的
torch 权重，运行不读。全部打包 487MB，只打必需的降到 258MB。

运行时定位顺序（插件 lineart/anyline.py 的 modelsDir）：
    环境变量 CANGAILINEART_MODELS
    → 插件内 models\\（开发机，零解压成本）
    → 程序根目录 ai_models.zip：自动解压到 %APPDATA%\\Cangai\\ai_models\\
所以最终用户只需把 ai_models.zip 复制到 Cangai.exe 同级目录，就能用上 AI 线稿。
"""
import os
import sys
import zipfile

MB = 1024 * 1024

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "ai_models.zip")

# 运行必需的权重（相对模型目录，zip 内用同样路径）
CORE = [
    "anime2sketch/A2S.onnx",
    "mobile_sam/mobile_sam.pt",
    "anyline/MTEED.onnx",
]

# 权重源目录：插件里（当前开发位置）优先，兼容根目录 models\
CANDIDATES = [
    os.path.join(ROOT, "plugins_private", "ai_lineart", "models"),
    os.path.join(ROOT, "plugins", "ai_lineart", "models"),
    os.path.join(ROOT, "models"),
]


def findSrc() -> str:
    for d in CANDIDATES:
        if os.path.exists(os.path.join(d, "anime2sketch", "A2S.onnx")):
            return d
    sys.exit("[ERROR] 找不到权重目录（期望 anime2sketch/A2S.onnx）:\n  " +
             "\n  ".join(CANDIDATES))


def listing(path: str) -> None:
    with zipfile.ZipFile(path) as zf:
        for i in zf.infolist():
            print(f"  {i.file_size / MB:9.1f} MB  {i.filename}")
        print(f"  zip 本体：{os.path.getsize(path) / MB:.1f} MB")


def main() -> None:
    if "-l" in sys.argv:
        if not os.path.exists(OUT):
            sys.exit(f"[ERROR] 还没有 {OUT}")
        listing(OUT)
        return

    src = findSrc()
    print(f"权重源：{src}")
    stored = "--stored" in sys.argv
    comp = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    total = 0
    tmp = OUT + ".part"
    with zipfile.ZipFile(tmp, "w", comp,
                         compresslevel=(0 if stored else 6)) as zf:
        for rel in CORE:
            p = os.path.join(src, rel.replace("/", os.sep))
            if not os.path.exists(p):
                sys.exit(f"[ERROR] 缺少权重：{p}")
            size = os.path.getsize(p)
            total += size
            print(f"  + {rel}  {size / MB:.1f} MB")
            zf.write(p, rel)
        zf.writestr("manifest.txt",
                    "# AI 线稿运行必需权重（由 pack_models.py 生成）\n" +
                    "\n".join(CORE) + "\n")
    os.replace(tmp, OUT)
    print(f"[OK] {os.path.basename(OUT)}：{os.path.getsize(OUT) / MB:.1f} MB"
          f"（原文件 {total / MB:.1f} MB）")
    print("     分发时把这个文件放到 Cangai.exe 同级目录，程序首次识别时自动解压。")


if __name__ == "__main__":
    main()
