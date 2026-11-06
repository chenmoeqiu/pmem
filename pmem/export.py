"""导出层：人类可读、可迁移。

两个格式：
- JSONL：机器可读，字段完整，适合将来迁移到别的工具
- Markdown：人可读，像笔记一样翻

导出的是"当前状态"：state='purged' 的消息不会出现。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import clock
from .config import Config


def _conversations(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT * FROM conversations
            WHERE state = 'active'
            ORDER BY COALESCE(started_at, '9999') ASC, id ASC"""
    ).fetchall()


def _messages(con: sqlite3.Connection, conversation_id: int) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT id, seq, role, origin, content, created_at, time_precision, source
             FROM messages
            WHERE conversation_id = ? AND state = 'active'
            ORDER BY seq ASC""",
        (conversation_id,),
    ).fetchall()


def export_jsonl(con: sqlite3.Connection, cfg: Config, out_path: Path) -> int:
    off = cfg.get("runtime", "utc_offset_hours", default=8)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        for c in _conversations(con):
            for m in _messages(con, int(c["id"])):
                atts = [
                    dict(a)
                    for a in con.execute(
                        """SELECT orig_name, sha256, path, bytes, mime
                             FROM attachments
                            WHERE message_id = ? AND state = 'active'""",
                        (m["id"],),
                    ).fetchall()
                ]
                rec = {
                    "conversation": {
                        "source": c["source"],
                        "source_id": c["source_id"],
                        "url": c["url"],
                        "title": c["title"],
                        "started_at": c["started_at"],
                        "time_precision": c["time_precision"],
                    },
                    "seq": m["seq"],
                    "role": m["role"],
                    "origin": m["origin"],
                    "content": m["content"],
                    "created_at": m["created_at"],
                    "created_at_local": clock.local_str(m["created_at"], off),
                    "time_precision": m["time_precision"],
                    "attachments": atts,
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
    return n


def _slug(title: str | None, source_id: str) -> str:
    base = (title or "untitled").strip().replace("/", "_").replace("\\", "_")
    base = "".join(ch for ch in base if ch not in '<>:"|?*\n\r\t')[:40]
    return f"{clock.parse_iso(None) or ''}{base}_{source_id[:8]}".strip() or source_id[:8]


def export_markdown(con: sqlite3.Connection, cfg: Config, out_dir: Path) -> int:
    off = cfg.get("runtime", "utc_offset_hours", default=8)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = 0
    for c in _conversations(con):
        msgs = _messages(con, int(c["id"]))
        if not msgs:
            continue
        started = clock.local_str(c["started_at"], off) if c["started_at"] else "未知"
        day = started[:10] if started != "未知时间" and len(started) >= 10 else "unknown"
        name = f"{day}_{(c['title'] or 'untitled')[:40]}_{c['source_id'][:8]}.md"
        name = "".join(ch for ch in name if ch not in '<>:"|?*\n\r\t/\\')
        path = out_dir / name

        lines = [f"# {c['title'] or '(无标题)'}", ""]
        lines.append(f"- 来源：{c['source']} / `{c['source_id']}`")
        if c["url"]:
            lines.append(f"- 链接：{c['url']}")
        lines.append(f"- 起始：{started}")
        lines.append(f"- 时间精度：{c['time_precision']}")
        lines.append(f"- 消息数：{len(msgs)}")
        lines.append("")
        lines.append("---")
        lines.append("")

        for m in msgs:
            who = {"user": "👤 用户", "assistant": "🤖 助手", "system": "⚙️ 系统"}.get(
                m["role"], m["role"]
            )
            when = clock.local_str(m["created_at"], off)
            lines.append(f"## {m['seq']}. {who} · {when}")
            lines.append("")
            lines.append(m["content"])
            for a in con.execute(
                """SELECT orig_name, sha256, path, bytes FROM attachments
                    WHERE message_id = ? AND state = 'active'""",
                (m["id"],),
            ).fetchall():
                lines.append("")
                lines.append(f"> 附件：`{a['orig_name']}` "
                             f"({a['bytes']} 字节, sha256:{a['sha256'][:12]}) "
                             f"— `{a['path']}`")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        files += 1
    return files
