"""闲时整理：把「转冷队列」消化掉。

转冷的语义就是「这一段结束了，可以整理了」——所以它**应该自己发生**，
而不是等人想起来敲命令。但打标是模型调用，不能塞进对话轮次里等它。

所以整理挂在这里，有三个入口：
1. 一轮对话结束（`chat._add` 写完助手那条、`ports.note` 写完助手那条）→ `maybe_auto`
2. 人工指令 `pmem daily` → `run`
3. 将来任何定时任务 → 同样调 `run`

`maybe_auto` 自己会判断该不该动，代价是**几毫秒的 SQL**（队列空就直接返回）：
- 队列空 → 不动
- 距上次整理不到 `interval_hours`（默认 6）→ 不动
- 手上没有能用的模型（离线/没有 key）→ 不动，队列留着下次

真正处理时按 `limit`（默认 2 场）限制，剩下的留给下一轮——这样一次对话最多多烧
2 次调用，不会因为"攒了一周"而一次爆掉。失败的会话留在队列里，下次重试。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import clock, search, tagging

# 上次真正整理完的时间（meta 表）。用来实现"间隔内不重复跑"。
META_KEY = "cold_last_run_at"


def _last_run(con: sqlite3.Connection) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
    return str(row[0]) if row else None


def pending_count(con: sqlite3.Connection) -> int:
    return int(con.execute(
        "SELECT COUNT(*) FROM cold_queue WHERE done_at IS NULL").fetchone()[0])


def due(con: sqlite3.Connection, cfg) -> tuple[bool, str]:
    """现在该不该整理。返回 (是否该跑, 一句话原因)。"""
    if not bool(cfg.get("cold", "auto", default=True)):
        return False, "自动整理已关闭（[cold] auto = false）"
    if pending_count(con) == 0:
        return False, "队列是空的"
    last = _last_run(con)
    interval = float(cfg.get("cold", "interval_hours", default=6) or 0)
    if last and interval > 0:
        a, b = clock.parse_iso(last), clock.parse_iso(clock.now_utc_iso())
        if a and b:
            hours = (b - a).total_seconds() / 3600.0
            if hours < interval:
                return False, f"距上次整理 {hours:.1f} 小时，还没到 {interval:g} 小时"
    return True, "可以整理"


def _usable(provider) -> bool:
    """能真正打标吗？离线 provider 会返回空结果，用它跑等于把会话标成"已整理"却没标签。"""
    return (provider is not None
            and hasattr(provider, "tag_batch")
            and getattr(provider, "name", "") != "offline")


def run(con: sqlite3.Connection, cfg, provider, *, limit: int | None = None) -> dict[str, Any]:
    """真的整理：先按边界入队，再消费队列。**幂等**，可以随便重复调用。"""
    if not _usable(provider):
        return {"ok": False, "reason": "没有可用的模型（离线/无 key），队列未动",
                "pending": pending_count(con)}

    since = search.segment_start_iso(con, cfg)
    enqueued = tagging.mark_cold_before(con, since) if since else 0

    n = int(limit if limit is not None else cfg.get("cold", "limit", default=2))
    stat = tagging.run_pending(con, cfg, provider, limit=n)

    # 只有真的处理过才盖时间戳：否则"队列空但盖了章"会让下一轮该跑的时候被间隔挡住
    if int(stat.get("conversations") or 0) > 0:
        con.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (META_KEY, clock.now_utc_iso()))

    return {"ok": True, "enqueued": enqueued, "limit": n,
            "processed": int(stat.get("conversations") or 0),
            "failed": int(stat.get("failed") or 0),
            "segments": int(stat.get("segments") or 0),
            "tags": int(stat.get("tags") or 0),
            "pending": pending_count(con)}


def maybe_auto(con: sqlite3.Connection, cfg, provider=None) -> dict[str, Any]:
    """对话结束时的钩子：该跑就跑，不该跑立刻返回。**绝不抛异常**给调用方。"""
    ok, why = due(con, cfg)
    if not ok:
        return {"ok": True, "skipped": why}

    # 只有调用方**根本没给** provider 时才自己造一个（例如 IM 插件只负责写）。
    # 给了但不能用（离线），就按调用方的意思跳过——不要在背后换成在线模型去烧调用。
    if provider is None:
        try:
            from . import llm
            provider = llm.build(cfg)
        except Exception:
            provider = None
    if not _usable(provider):
        return {"ok": True, "skipped": "没有可用的模型（离线/无 key），队列留着"}

    return run(con, cfg, provider)
