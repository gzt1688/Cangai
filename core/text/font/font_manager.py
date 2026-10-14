"""字体管理器：按扩展名分派加载、缓存、列举内置与系统字体。"""
from __future__ import annotations

import os
import sys
import glob
import hashlib
import pickle
import zlib
from typing import Dict, List, Optional, Tuple
from .font_data import FontData, FontType
from . import ttf_reader, sgl_reader, shx_reader, shp_reader


def _resourcesFontDir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(3):
        here = os.path.dirname(here)
    return os.path.join(here, "resources", "fonts")


def _systemFontDirs() -> List[str]:
    """Windows 系统字体目录 + 用户字体目录。"""
    dirs: List[str] = []
    if sys.platform.startswith("win"):
        dirs.append(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    return dirs


def _readFontFamilyNames(path: str) -> List[str]:
    """读字体真实族名；优先中文 locale（langID=0x804），.ttc 展开所有子字体。"""
    try:
        from fontTools.ttLib import TTFont
    except Exception:
        return []
    names: List[str] = []
    if path.lower().endswith(".ttc"):
        i = 0
        while i < 64:
            try:
                tt = TTFont(path, fontNumber=i, lazy=True)
                nm = _familyNameChinese(tt)
                if nm:
                    names.append(nm)
                i += 1
            except Exception:
                break
        return names
    try:
        tt = TTFont(path, fontNumber=0, lazy=True)
        nm = _familyNameChinese(tt)
        if nm:
            names.append(nm)
    except Exception:
        pass
    return names


def _familyNameChinese(tt) -> Optional[str]:
    """优先返回中文 locale 的族名（nameID=1, langID=0x804/0x404），
    退而求其次取任何含中文的族名，最后回退 getDebugName(1)。"""
    try:
        nt = tt["name"]
    except Exception:
        return None
    for rec in nt.names:
        if rec.nameID == 1 and rec.platformID == 3 and rec.langID in (0x804, 0x404):
            nm = rec.toUnicode()
            if nm:
                return nm
    for rec in nt.names:
        if rec.nameID == 1:
            nm = rec.toUnicode()
            if nm and any(0x4e00 <= ord(c) <= 0x9fff for c in nm):
                return nm
    nm = nt.getDebugName(1)
    return nm or None


def _splitFontNumber(filePath: str) -> Tuple[str, int]:
    """解析 'path#n' 为 (path, n)；# 前扩展名为 ttf/otf/ttc 才拆分。"""
    if "#" in filePath:
        base, _, num = filePath.rpartition("#")
        ext = os.path.splitext(base)[1].lower()
        if ext in (".ttc", ".ttf", ".otf"):
            try:
                return base, int(num)
            except ValueError:
                pass
    return filePath, 0


def loadFont(filePath: str) -> FontData:
    realPath, fontNumber = _splitFontNumber(filePath)
    ext = os.path.splitext(realPath)[1].lower()
    if ext in (".sgl",):
        # sgl 单线字体解析约 5~11s，用磁盘缓存（pickle+zlib，按文件
        # mtime/size 校验）避免每次启动/文字命令重复解析。unpickle
        # 失败（代码更新类结构变化等）自动回退重新解析并重建缓存。
        cached = _cachedFont(realPath)
        if cached is not None:
            return cached
        fd = sgl_reader.loadFromFile(realPath)
        if fd is not None and fd.isLoaded and not fd.error:
            _saveCachedFont(realPath, fd)
        return fd
    if ext in (".shx",):
        return shx_reader.loadFromFile(realPath)
    if ext in (".shp",):
        return shp_reader.loadFromFile(realPath)
    if ext in (".ttf", ".otf", ".ttc"):
        return ttf_reader.loadFromFile(realPath, fontNumber)
    fd = FontData()
    fd.error = f"不支持的字体格式: {ext}"
    return fd



# ---- sgl 字体磁盘缓存 ----
_FONT_CACHE_DIR = None


def _fontCacheDir() -> Optional[str]:
    global _FONT_CACHE_DIR
    if _FONT_CACHE_DIR is None:
        d = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "Cangai", "font_cache")
        try:
            os.makedirs(d, exist_ok=True)
            _FONT_CACHE_DIR = d
        except Exception:
            _FONT_CACHE_DIR = ""
    return _FONT_CACHE_DIR or None


def _cachedFont(realPath: str) -> Optional[FontData]:
    d = _fontCacheDir()
    if not d:
        return None
    try:
        st = os.stat(realPath)
        key = hashlib.md5(realPath.encode("utf-8")).hexdigest()
        p = os.path.join(d, key + ".pkl.z")
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            blob = zlib.decompress(f.read())
        meta, fd = pickle.loads(blob)
        if meta != (st.st_mtime_ns, st.st_size):
            return None
        return fd
    except Exception:
        return None


def _saveCachedFont(realPath: str, fd: FontData) -> None:
    d = _fontCacheDir()
    if not d:
        return
    try:
        st = os.stat(realPath)
        key = hashlib.md5(realPath.encode("utf-8")).hexdigest()
        p = os.path.join(d, key + ".pkl.z")
        blob = zlib.compress(pickle.dumps(((st.st_mtime_ns, st.st_size), fd)), 6)
        with open(p, "wb") as f:
            f.write(blob)
    except Exception:
        pass


class FontManager:
    _instance: "Optional[FontManager]" = None

    def __init__(self) -> None:
        self._cache: Dict[str, FontData] = {}
        self._builtinDir = _resourcesFontDir()

    @classmethod
    def instance(cls) -> "FontManager":
        if cls._instance is None:
            cls._instance = FontManager()
        return cls._instance

    def builtinDir(self) -> str:
        return self._builtinDir

    def listBuiltinFonts(self) -> List[Tuple[str, str]]:
        """返回 [(显示名, 路径)]，内置字体目录下全部支持的字体。"""
        out = []
        if not os.path.isdir(self._builtinDir):
            return out
        for ext in ("*.sgl", "*.shx", "*.shp", "*.ttf", "*.otf", "*.ttc"):
            for p in glob.glob(os.path.join(self._builtinDir, ext)):
                out.append((os.path.splitext(os.path.basename(p))[0], p))
        return out

    def listSystemFonts(self) -> List[Tuple[str, str]]:
        """返回 [(显示名, 路径)]，系统已安装 TTF/OTF/TTC（仅 Windows）。
        用 fontTools 读字体真实族名（中文），.ttc 集合展开每个子字体。
        扫描系统字体目录和用户字体目录，按路径去重。"""
        out = []
        seen = set()
        for d in _systemFontDirs():
            if not d or not os.path.isdir(d):
                continue
            for ext in ("*.ttf", "*.otf", "*.ttc"):
                for p in glob.glob(os.path.join(d, ext)):
                    if p in seen:
                        continue
                    seen.add(p)
                    names = _readFontFamilyNames(p)
                    if not names:
                        out.append((os.path.splitext(os.path.basename(p))[0], p))
                    elif len(names) == 1:
                        out.append((names[0], p))
                    else:
                        for i, nm in enumerate(names):
                            out.append((nm, f"{p}#{i}"))
        out.sort(key=lambda np: (
            0 if any(0x4e00 <= ord(c) <= 0x9fff for c in np[0]) else 1,
            np[0],
        ))
        return out

    def get(self, filePath: str) -> FontData:
        key = os.path.normpath(filePath)
        fd = self._cache.get(key)
        if fd is not None and fd.isLoaded:
            return fd
        fd = loadFont(filePath)
        self._cache[key] = fd
        return fd

    def getCached(self, filePath: str) -> Optional[FontData]:
        """返回已缓存的字体；未加载则返回 None（不触发加载）。"""
        fd = self._cache.get(os.path.normpath(filePath))
        if fd is not None and fd.isLoaded:
            return fd
        return None

    def clearCache(self) -> None:
        self._cache.clear()