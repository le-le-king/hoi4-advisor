"""投递记录与"下次存档核对"。

游戏不会回报命令执行结果——控制台失败只在屏幕左上角飘一行红字，
DLL 看不到（这是写侧最根本的限制）。所以只剩一条路能确认：
**下一条命令之后的下一次存档，看那个数有没有变。**

这个模块就是干这个的：
- 发命令时记一条 baseline（发之前存档里的值）
- 下次存档解析完，拿新值减 baseline，给一个诚实的结论
- 结论分四档：确实加了 / 加了一部分 / 没看到变化 / 核对不了

不做假承诺：宁可说"没看到变化，可能这档是发命令之前存的"，
也不说"已生效"——那是猜的。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

__all__ = ["add", "pending", "reconcile", "recent", "clear", "label_of"]

LOG_NAME = "act_log.json"
MAX_ITEMS = 40
# 存档比命令晚这么多秒，才算"这条命令有可能已经进了这份档"
FRESH_MARGIN = 5.0

_STATE_TEXT = {
    "pending": "等下次存档核对",
    "confirmed": "存档里确认到账",
    "partial": "只到了一部分",
    "none": "存档里没看到变化",
    "capped": "已经到顶，看不出全部效果",
    "unverifiable": "这类操作没法用存档核对",
}

_CAP_FIELDS = {"稳定度", "战争支持"}


def label_of(state: str) -> str:
    return _STATE_TEXT.get(state, state)


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / LOG_NAME


def _read(data_dir: Path) -> list[dict]:
    path = _path(data_dir)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("items") if isinstance(raw, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _write(data_dir: Path, items: list[dict]) -> bool:
    path = _path(data_dir)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": time.time(), "items": items[:MAX_ITEMS]}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def add(data_dir: Path, record: dict) -> dict:
    """记一条投递。record 由调用方准备好，这里只补 id / 状态 / 落盘。"""
    rec = dict(record)
    rec.setdefault("ts", time.time())
    rec["id"] = f"{int(rec['ts'] * 1000)}"
    if rec.get("baseline") is None or not rec.get("field"):
        rec["state"] = "unverifiable"
    else:
        rec.setdefault("state", "pending")
    items = _read(data_dir)
    items.insert(0, rec)
    _write(data_dir, items)
    return rec


def pending(data_dir: Path) -> list[dict]:
    return [i for i in _read(data_dir) if i.get("state") == "pending"]


def recent(data_dir: Path, n: int = 3) -> list[dict]:
    return _read(data_dir)[:n]


def clear(data_dir: Path) -> None:
    _write(data_dir, [])


def reconcile(data_dir: Path, snap: dict) -> list[dict]:
    """存档解析完之后调一次，把能判的都判掉，返回**这次刚有结论的**那几条。

    判据：
    - 存档解析时间要比发命令的时间晚（否则这份档肯定是发命令之前的）
    - baseline 有、字段能读到
    - 加量型：差值够 85% 算到账；几乎没动算没到账；中间算只到一部分
    - 稳定度/战争支持有 100 上限，到顶了只能说"看不出全部效果"
    """
    items = _read(data_dir)
    if not items:
        return []

    player = snap.get("player") or {}
    meta = snap.get("meta") or {}
    parsed_at = float(meta.get("parsed_at") or 0)
    save_date = str(meta.get("save_date") or "")
    if parsed_at <= 0:
        return []

    judged: list[dict] = []
    changed = False
    for rec in items:
        if rec.get("state") != "pending":
            continue
        if float(rec.get("ts") or 0) + FRESH_MARGIN > parsed_at:
            continue          # 命令是在这份档之后发的，等下一份
        field = rec.get("field") or ""
        baseline = rec.get("baseline")
        now = player.get(field)
        if not field or baseline is None or not isinstance(now, (int, float)):
            continue

        amount = float(rec.get("amount") or 0)
        delta = float(now) - float(baseline)
        if amount > 0:
            if delta >= amount * 0.85 - 1:
                state = "confirmed"
            elif delta <= max(1.0, amount * 0.15):
                state = "none"
            else:
                state = "partial"
        else:
            state = "unverifiable"

        if state == "partial" and field in _CAP_FIELDS and float(now) >= 99 and float(baseline) >= 90:
            state = "capped"

        rec["state"] = state
        rec["result_value"] = round(float(now), 1)
        rec["result_delta"] = round(delta, 1)
        rec["checked_at"] = time.time()
        rec["checked_save_date"] = save_date
        changed = True
        judged.append(rec)

    if changed:
        _write(data_dir, items)
    return judged
