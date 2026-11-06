"""图片描述：让图片在记忆里"存在"。

设计约束（用户 2026-11-06）：

- **只写一句话描述，不替代原文**。图片本体和原始附件永远保留；
  描述是派生层，可以重建、可以删、可以重跑。
- **不扫全库是常态**。`describe` 显式给 `--limit`/`--id`，一次只处理你点的那些；
  随机抽一两张验证就够，不需要把 682 张全跑一遍。
- 描述写进 `attachments.description`，随后能被 `pmem search --images` 查到，
  也能进对话链路（作为"相关图片"块附在证据后面，不占证据编号）。

调用失败不抛：一张图失败只记一条失败原因，继续下一张——批量任务不能因为一张图中断。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import clock

DESC_HINT = (
    "用一句中文描述这张图里有什么，只写可检索的事实："
    "场景、物体、屏幕上的文字/数字、地点线索、几个人。"
    "不要评价、不要猜测用途、不要写'这可能是在……'。"
)


def pending(con: sqlite3.Connection, *, limit: int = 5,
            ids: list[int] | None = None) -> list[dict[str, Any]]:
    """挑还没描述的图片。给了 ids 就只处理这些。"""
    if ids:
        q = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT id, path, mime, orig_name FROM attachments "
            f"WHERE id IN ({q}) AND state = 'active'", ids).fetchall()
    else:
        rows = con.execute(
            "SELECT id, path, mime, orig_name FROM attachments "
            "WHERE state = 'active' AND kind = 'image' "
            "AND (description IS NULL OR description = '') "
            "ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [{"id": int(r["id"]), "path": r["path"], "mime": r["mime"] or "",
             "orig_name": r["orig_name"]} for r in rows]


def describe_batch(con: sqlite3.Connection, cfg, provider, *,
                   limit: int = 5, ids: list[int] | None = None) -> dict[str, Any]:
    """给若干张图写描述并落库。返回统计，不抛异常。"""
    media = cfg.path("paths", "media")
    todo = pending(con, limit=limit, ids=ids)
    out = {"tried": 0, "described": 0, "skipped": 0, "failed": 0,
           "items": [], "errors": []}
    for a in todo:
        p = Path(media) / a["path"]
        if not p.exists():
            out["failed"] += 1
            out["errors"].append(f"#{a['id']} 文件不存在：{a['path']}")
            continue
        out["tried"] += 1
        try:
            text, _usage = provider.describe_image(
                p.read_bytes(), a["mime"] or "image/jpeg", hint=DESC_HINT)
        except Exception as e:  # noqa: BLE001
            out["failed"] += 1
            out["errors"].append(f"#{a['id']} 调用失败：{type(e).__name__}")
            continue
        text = (text or "").strip().replace("\n", " ")[:200]
        if not text:
            out["skipped"] += 1
            continue
        con.execute("UPDATE attachments SET description = ?, described_at = ? WHERE id = ?",
                    (text, clock.now_utc_iso(), a["id"]))
        out["described"] += 1
        out["items"].append({"id": a["id"], "description": text})
    return out


def search(con: sqlite3.Connection, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """按描述找图。简单子串匹配——先能用，之后要上 FTS 再说。"""
    q = (query or "").strip()
    if not q:
        return []
    rows = con.execute(
        """SELECT a.id, a.path, a.orig_name, a.description, a.created_at,
                  a.conversation_id, a.message_id, c.title AS conversation_title
             FROM attachments a
             LEFT JOIN conversations c ON c.id = a.conversation_id
            WHERE a.state = 'active' AND a.description LIKE ?
            ORDER BY a.created_at DESC LIMIT ?""",
        (f"%{q}%", limit)).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def render(rows: list[dict[str, Any]], budget: int = 300) -> str:
    """渲染成给模型看的"相关图片"块。不占证据编号。"""
    if not rows:
        return ""
    lines = []
    for r in rows[:5]:
        lines.append(f"- [{r['created_at'][:10]}] {r['description']}"
                     f"（图 {r['id']}，题《{(r['conversation_title'] or '')[:20]}》）")
    text = "\n".join(lines)
    return text[:budget]
