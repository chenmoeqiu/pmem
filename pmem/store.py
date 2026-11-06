"""写入层：会话、消息、附件。

原始层（conversations / messages / attachments）只追加。
附件按 sha256 内容寻址落盘，同一张图重复出现只存一份。
"""

from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
from pathlib import Path

from . import clock, text

VIDEO_EXT = {".mov", ".mp4", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".tif", ".tiff"}
DOC_EXT = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".txt", ".md",
           ".csv", ".json", ".py", ".zip", ".epub"}


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in DOC_EXT:
        return "document"
    return "other"


def upsert_conversation(
    con: sqlite3.Connection,
    *,
    source: str,
    source_id: str,
    url: str | None = None,
    title: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    time_precision: str = "unknown",
) -> int:
    """按 source_id 合并会话。时间取"最早开始 / 最晚结束"，精度取最强的一档。"""
    row = con.execute(
        "SELECT * FROM conversations WHERE source_id = ?", (source_id,)
    ).fetchone()

    if row:
        starts = [v for v in (row["started_at"], started_at) if v]
        ends = [v for v in (row["ended_at"], ended_at) if v]
        prec = (
            "message"
            if "message" in (row["time_precision"], time_precision)
            else (row["time_precision"] if row["time_precision"] != "unknown" else time_precision)
        )
        con.execute(
            """UPDATE conversations
                  SET title = ?, url = ?, started_at = ?, ended_at = ?, time_precision = ?
                WHERE id = ?""",
            (row["title"] or title, row["url"] or url,
             min(starts) if starts else None, max(ends) if ends else None,
             prec, row["id"]),
        )
        return int(row["id"])

    cur = con.execute(
        """INSERT INTO conversations
               (source, source_id, url, title, started_at, ended_at,
                time_precision, state, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
        (source, source_id, url, title, started_at, ended_at,
         time_precision, clock.now_utc_iso()),
    )
    return int(cur.lastrowid)


def next_seq(con: sqlite3.Connection, conversation_id: int) -> int:
    row = con.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    return int(row["m"]) + 1


def add_message(
    con: sqlite3.Connection,
    *,
    conversation_id: int,
    role: str,
    content: str,
    created_at: str | None,
    time_precision: str,
    source: str,
    source_ref: str | None = None,
    origin: str | None = None,
    seq: int | None = None,
) -> int:
    if seq is None:
        seq = next_seq(con, conversation_id)
    origin = origin or role
    cur = con.execute(
        """INSERT INTO messages
               (conversation_id, seq, role, origin, content, content_indexed,
                created_at, time_precision, source, source_ref, state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (conversation_id, seq, role, origin, content, text.tokenize(content),
         created_at, time_precision, source, source_ref),
    )
    return int(cur.lastrowid)


def find_existing_message(
    con: sqlite3.Connection, conversation_id: int, role: str, content: str
) -> int | None:
    """按归一化内容查找同一条消息，用于跨数据源合并（第二批用）。"""
    norm = text.normalize_for_key(content)
    rows = con.execute(
        "SELECT id, content FROM messages WHERE conversation_id = ? AND role = ? AND state = 'active'",
        (conversation_id, role),
    ).fetchall()
    for r in rows:
        if text.normalize_for_key(r["content"]) == norm:
            return int(r["id"])
    return None


def _ext_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ext if 2 <= len(ext) <= 8 else ""


def store_attachment(
    con: sqlite3.Connection,
    media_root: Path,
    *,
    data: bytes,
    orig_name: str,
    when_iso: str | None,
    message_id: int | None = None,
    conversation_id: int | None = None,
    utc_offset_hours: float = 8,
) -> tuple[int, str, bool]:
    """落盘并登记附件。返回 (attachment_id, 相对路径, 是否新增了记录行)。"""
    sha = hashlib.sha256(data).hexdigest()

    # 同一条消息重复导入时，不再新增附件行（重复导入必须幂等）
    if message_id is not None:
        dup = con.execute(
            "SELECT id, path FROM attachments WHERE message_id = ? AND sha256 = ? "
            "AND state = 'active' LIMIT 1",
            (message_id, sha),
        ).fetchone()
        if dup:
            return int(dup["id"]), dup["path"], False

    ext = _ext_of(orig_name) or mimetypes.guess_extension(
        mimetypes.guess_type(orig_name)[0] or ""
    ) or ".bin"

    hit = con.execute(
        "SELECT path FROM attachments WHERE sha256 = ? AND state = 'active' LIMIT 1", (sha,)
    ).fetchone()
    if hit:
        rel_path = hit["path"]
        is_new = False
    else:
        stamp, ymd = clock.local_stamp(when_iso, utc_offset_hours)
        rel_path = f"{ymd}/{stamp}_{sha[:8]}{ext}"
        is_new = True

    abs_path = media_root / rel_path
    if is_new or not abs_path.exists():
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(data)
        is_new = True

    cur = con.execute(
        """INSERT INTO attachments
               (message_id, conversation_id, kind, orig_name, sha256, path,
                bytes, mime, created_at, state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (message_id, conversation_id, kind_of(orig_name), orig_name, sha,
         rel_path.replace("\\", "/"), len(data),
         mimetypes.guess_type(orig_name)[0], clock.now_utc_iso()),
    )
    return int(cur.lastrowid), rel_path.replace("\\", "/"), is_new
