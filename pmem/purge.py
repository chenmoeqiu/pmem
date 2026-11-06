"""purge：唯一的破坏性操作。

append-only 是底座原则，而"有些东西不该留"是它唯一的例外。
例外必须显式、可审计、且穿透所有副本：
  原文 -> FTS 索引（触发器同步）-> 派生数据（外键级联）-> 附件 -> 不保留内容

审计只记录"哪个 id 在何时被清除"，绝不留内容。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import clock


def _delete_files_if_orphan(con: sqlite3.Connection, media_root: Path, rows) -> int:
    removed = 0
    for r in rows:
        still = con.execute(
            "SELECT COUNT(*) FROM attachments WHERE path = ? AND state = 'active'",
            (r["path"],),
        ).fetchone()[0]
        if still == 0:
            p = media_root / r["path"]
            if p.exists():
                p.unlink()
                removed += 1
    return removed


def purge_message(
    con: sqlite3.Connection,
    media_root: Path,
    message_id: int,
    *,
    hard_files: bool = False,
    note: str | None = None,
) -> dict:
    row = con.execute(
        "SELECT id, conversation_id FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if not row:
        return {"ok": False, "reason": "message not found"}

    atts = con.execute(
        "SELECT path FROM attachments WHERE message_id = ? AND state = 'active'",
        (message_id,),
    ).fetchall()

    con.execute(
        """UPDATE messages
              SET content = '', content_indexed = '', state = 'purged'
            WHERE id = ?""",
        (message_id,),
    )
    con.execute(
        "UPDATE attachments SET state = 'purged' WHERE message_id = ?", (message_id,)
    )
    removed = _delete_files_if_orphan(con, media_root, atts) if hard_files else 0
    con.execute(
        "INSERT INTO purge_log(target_kind, target_id, purged_at, note) VALUES (?,?,?,?)",
        ("message", message_id, clock.now_utc_iso(), note),
    )
    return {"ok": True, "messages": 1, "attachments": len(atts), "files_removed": removed}


def purge_conversation(
    con: sqlite3.Connection,
    media_root: Path,
    conversation_id: int,
    *,
    hard_files: bool = False,
    note: str | None = None,
) -> dict:
    n_msg = con.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND state = 'active'",
        (conversation_id,),
    ).fetchone()[0]
    atts = con.execute(
        "SELECT path FROM attachments WHERE conversation_id = ? AND state = 'active'",
        (conversation_id,),
    ).fetchall()

    con.execute(
        """UPDATE messages
              SET content = '', content_indexed = '', state = 'purged'
            WHERE conversation_id = ?""",
        (conversation_id,),
    )
    con.execute(
        "UPDATE attachments SET state = 'purged' WHERE conversation_id = ?",
        (conversation_id,),
    )
    con.execute(
        "UPDATE conversations SET state = 'purged' WHERE id = ?", (conversation_id,)
    )
    con.execute(
        "DELETE FROM memory_tags WHERE message_id IN "
        "(SELECT id FROM messages WHERE conversation_id = ?)",
        (conversation_id,),
    )
    removed = _delete_files_if_orphan(con, media_root, atts) if hard_files else 0
    con.execute(
        "INSERT INTO purge_log(target_kind, target_id, purged_at, note) VALUES (?,?,?,?)",
        ("conversation", conversation_id, clock.now_utc_iso(), note),
    )
    return {"ok": True, "messages": n_msg, "attachments": len(atts), "files_removed": removed}
