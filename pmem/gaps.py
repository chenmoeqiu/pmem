"""导入缺口：有引用、包内没有实体文件。

设计立场：宁可承认丢了什么，也不悄悄吞掉。
这些记录将来可以被手工补（`pmem attach`），补上之后图片描述等派生流程会自动接上。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import clock, store
from .config import Config


def record(
    con: sqlite3.Connection,
    *,
    conversation_id: int | None,
    message_id: int | None,
    source: str,
    orig_name: str,
    reason: str = "not_in_archive",
) -> None:
    con.execute(
        """INSERT INTO import_gaps
               (conversation_id, message_id, source, orig_name, reason, status, created_at)
           VALUES (?,?,?,?,?, 'open', ?)""",
        (conversation_id, message_id, source, orig_name, reason, clock.now_utc_iso()),
    )


def list_gaps(
    con: sqlite3.Connection, *, status: str = "open", limit: int = 300
) -> list[dict]:
    rows = con.execute(
        """SELECT g.id, g.orig_name, g.reason, g.status, g.created_at, g.message_id,
                  g.conversation_id, c.title AS conversation_title
             FROM import_gaps g
             LEFT JOIN conversations c ON c.id = g.conversation_id
            WHERE (? = 'all' OR g.status = ?)
            ORDER BY g.id ASC LIMIT ?""",
        (status, status, limit),
    ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def summary(con: sqlite3.Connection) -> dict:
    rows = con.execute(
        "SELECT status, COUNT(*) n FROM import_gaps GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def attach_manual(
    con: sqlite3.Connection,
    cfg: Config,
    *,
    message_id: int,
    file_path: Path,
) -> dict:
    """把用户手工找到的文件补到指定消息上，并把对应的缺口标成已补。"""
    row = con.execute(
        "SELECT id, conversation_id, created_at FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "reason": "message not found"}
    if not file_path.exists():
        return {"ok": False, "reason": f"file not found: {file_path}"}

    data = file_path.read_bytes()
    att_id, rel, created = store.store_attachment(
        con, cfg.path("paths", "media"),
        data=data, orig_name=file_path.name,
        when_iso=row["created_at"], message_id=message_id,
        conversation_id=row["conversation_id"],
        utc_offset_hours=float(cfg.get("runtime", "utc_offset_hours", default=8)),
    )

    # 优先匹配同名缺口，否则补掉这条消息上任意一个未决缺口
    same = con.execute(
        "SELECT id FROM import_gaps WHERE message_id = ? AND orig_name = ? AND status = 'open' LIMIT 1",
        (message_id, file_path.name),
    ).fetchone()
    target = same or con.execute(
        "SELECT id FROM import_gaps WHERE message_id = ? AND status = 'open' LIMIT 1",
        (message_id,),
    ).fetchone()
    if target:
        con.execute(
            "UPDATE import_gaps SET status = 'filled', filled_attachment_id = ? WHERE id = ?",
            (att_id, target["id"]),
        )

    return {"ok": True, "attachment_id": att_id, "path": rel,
            "bytes": len(data), "gap_filled": bool(target), "created": created}
