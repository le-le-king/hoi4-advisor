"""缓存读写。工具只读这里，永远不碰解析。

原子落盘：先写 .tmp 再 os.replace。工具随时可能在读，
写一半被读到会得到半个 JSON —— 宁可读到旧版本，不能读到坏文件。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

__all__ = ["cache_path", "read_cache", "write_cache", "cache_matches_save"]

CACHE_NAME = "state_cache.json"


def cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / CACHE_NAME


def read_cache(data_dir: Path) -> dict | None:
    path = cache_path(data_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def write_cache(data_dir: Path, snap: dict) -> bool:
    """原子写。返回是否成功。"""
    path = cache_path(data_dir)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def cache_matches_save(cache: dict | None, save_file: Path | None) -> bool:
    """缓存是不是当前这份存档的产物。"""
    if not cache or save_file is None:
        return False
    meta = cache.get("meta") or {}
    try:
        target = str(Path(save_file).resolve()).lower()
    except OSError:
        target = str(save_file).lower()
    if str(meta.get("save_path", "")).lower() != target:
        return False
    try:
        st = Path(save_file).stat()
    except OSError:
        return False
    if int(meta.get("save_size", -1)) != st.st_size:
        return False
    # mtime 用 1 秒容差，避免文件系统精度差异导致的假失效
    return abs(float(meta.get("save_mtime", 0)) - st.st_mtime) < 1.0


def age_seconds(cache: dict | None) -> float:
    if not cache:
        return -1.0
    parsed_at = (cache.get("meta") or {}).get("parsed_at")
    if not isinstance(parsed_at, (int, float)):
        return -1.0
    return max(0.0, time.time() - float(parsed_at))
