"""插件自己的日志。

为什么不用 self.logger：实测宿主的 logger 输出不进 N.E.K.O 的日志文件，
"功能一直空着、日志一片安静"就是这么来的。所以关键路径全部自己落盘。

规矩：只写插件目录下的 data/advisor.log，不写 C 盘、不写 TEMP。
"""

from __future__ import annotations

import time
from pathlib import Path

__all__ = ["write", "tail"]

MAX_BYTES = 1_000_000
LOG_NAME = "advisor.log"


def write(data_dir: Path, msg: str) -> None:
    """追加一行带时间戳的日志。写不进去也不抛（日志失败不能拖垮功能）。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / LOG_NAME
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def tail(data_dir: Path, n: int = 40) -> str:
    """读最后 n 行，给工具返回里带一点现场，省得用户来回翻文件。"""
    path = Path(data_dir) / LOG_NAME
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])
