"""Voyager（gemini-voyager）导出适配器（补充数据源）。

它提供 Voyager 遗漏的东西：助手的回复、轮次结构、导出时的图片资产。
它缺少的正是 Takeout 有的：每轮时间戳。所以两者按 URL JOIN。

已知事实（实测）：
- `chat.md` 里的 `**Date**` 是**导出时刻**，不是对话时间，必须忽略
- JSON 里只有 `exportedAt`，也是导出时刻
- JSON 里的图片是 lh3.googleusercontent.com 链接，会过期，不能用；
  必须用 ZIP 里的 `assets/` 本地文件
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from .. import clock, store
from ..config import Config

_TURN_RE = re.compile(r"^##\s*Turn\s+\d+", re.I)
_SPEAKER_RE = re.compile(r"^###\s*(.+?)\s*$")
_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_SRC_RE = re.compile(r"\*\*Source\*\*:\s*\[[^\]]*\]\(([^)]+)\)")
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")

USER_LABELS = {"用户", "user", "you", "我"}
ASSISTANT_LABELS = {"助手", "assistant", "gemini", "模型", "system", "系统"}


def _role_of(label: str) -> str | None:
    """严格识别发言人。

    助手回复里也大量使用 `###` 标题（例如 `### 一、可行性分析`），
    所以不能见到 `###` 就当成新发言人——只有标签本身是已知发言人时才算。
    """
    cleaned = label.strip()
    for ch in ("👤", "🤖", "⚙️", "⚙", "🧑", "🦾"):
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip().lower()
    if cleaned in USER_LABELS:
        return "user"
    if cleaned in ASSISTANT_LABELS:
        return "assistant" if cleaned not in {"system", "系统"} else "system"
    return None


def parse_md(md: str) -> dict[str, Any]:
    title = None
    url = None
    turns: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    for line in md.splitlines():
        if title is None:
            m = _TITLE_RE.match(line)
            if m:
                title = m.group(1)
                continue
        if url is None:
            m = _SRC_RE.search(line)
            if m:
                url = m.group(1)
                continue
        if _TURN_RE.match(line):
            continue
        m = _SPEAKER_RE.match(line)
        if m:
            role = _role_of(m.group(1))
            if role is not None:
                cur = {"role": role, "lines": []}
                turns.append(cur)
                continue
            # 不是发言人：这是助手回复里的 markdown 标题，属于正文
        if cur is not None:
            cur["lines"].append(line)

    out = []
    for t in turns:
        body = "\n".join(t["lines"])
        body = body.replace("*[This turn includes uploaded images]*", "")
        images = []
        for ref in _IMG_RE.findall(body):
            if ref.startswith("assets/"):
                images.append(ref)
        body = _IMG_RE.sub("", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        if body or images:
            out.append({"role": t["role"], "content": body, "images": images})
    return {"title": title, "url": url, "turns": out}


def import_voyager(
    con,
    cfg: Config,
    path: Path,
    *,
    at: str | None = None,
    date_only: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """导入一场 Voyager 对话。

    at / date_only：这场对话的时间（你从网页上读到的）。没有真时间就标 unknown，
    **绝不拿导出时间冒充**。
    """
    media_root = cfg.path("paths", "media")
    off = cfg.get("runtime", "utc_offset_hours", default=8)

    if at:
        dt = clock.parse_iso(at)
        if dt is None:
            raise SystemExit(f"无法解析时间: {at}")
        created_at = clock.to_iso_utc(dt)
        precision = "conversation"
    elif date_only:
        dt = clock.parse_iso(date_only + "T00:00:00+08:00")
        created_at = clock.to_iso_utc(dt) if dt else None
        precision = "conversation"
    else:
        created_at = None
        precision = "unknown"

    md = None
    z: zipfile.ZipFile | None = None
    meta: dict[str, Any] = {}

    if path.suffix.lower() == ".zip":
        z = zipfile.ZipFile(path)
        names = z.namelist()
        md_name = next((n for n in names if n.lower().endswith("chat.md")), None)
        if not md_name:
            raise SystemExit("ZIP 里没有 chat.md")
        md = z.read(md_name).decode("utf-8", errors="replace")
        root = md_name.rsplit("/", 1)[0] + "/" if "/" in md_name else ""
        for n in names:
            if n.lower().endswith(".json"):
                try:
                    meta = json.loads(z.read(n).decode("utf-8"))
                except Exception:
                    pass
                break
    else:
        md = path.read_text(encoding="utf-8", errors="replace")
        root = ""
        js = path.with_suffix(".json")
        if js.exists():
            try:
                meta = json.loads(js.read_text(encoding="utf-8"))
            except Exception:
                pass

    parsed = parse_md(md)
    url = parsed["url"] or meta.get("url")
    title = meta.get("title") or parsed["title"] or path.stem
    if not url:
        source_id = "voyager-" + hashlib.sha256(md.encode("utf-8")).hexdigest()[:16]
    else:
        source_id = url.rstrip("/").rsplit("/", 1)[-1]

    conv_id = store.upsert_conversation(
        con, source="voyager", source_id=source_id, url=url, title=title,
        started_at=created_at, ended_at=created_at, time_precision=precision,
    )

    stat = {"conversation_id": conv_id, "messages": 0, "attachments": 0,
            "skipped_existing": 0, "derived_time": 0}
    ordered_ids: list[int] = []
    last_user_time: str | None = None

    for t in parsed["turns"]:
        mid: int | None = None
        msg_time, msg_prec = created_at, precision

        # 两种角色都先查重：重复导入同一份导出不应该产生重复消息
        mid = store.find_existing_message(con, conv_id, t["role"], t["content"])
        if mid is not None:
            row = con.execute(
                "SELECT created_at, time_precision FROM messages WHERE id = ?", (mid,)
            ).fetchone()
            if row:
                msg_time, msg_prec = row["created_at"], row["time_precision"]
            stat["skipped_existing"] += 1
            if t["role"] == "user":
                last_user_time = msg_time
        elif t["role"] == "user":
            last_user_time = msg_time
        else:
            # 助手回复没有独立时间戳，但它必然晚于所回应的那条用户消息。
            # 用"前一条用户消息的时间"作为派生时间，标 precision='derived'，
            # 这样绝不会出现"回复早于提问"的倒挂。
            if last_user_time:
                msg_time, msg_prec = last_user_time, "derived"
                stat["derived_time"] += 1

        if mid is None:
            mid = store.add_message(
                con, conversation_id=conv_id,
                role=t["role"], origin=t["role"], content=t["content"],
                created_at=msg_time, time_precision=msg_prec,
                source="voyager", source_ref=None,
            )
            stat["messages"] += 1
        ordered_ids.append(mid)

        if z is not None:
            for ref in t["images"]:
                member = root + ref
                if member not in z.namelist():
                    continue
                data = z.read(member)
                _aid, _p, inserted = store.store_attachment(
                    con, media_root,
                    data=data, orig_name=Path(ref).name, when_iso=created_at,
                    message_id=mid, conversation_id=conv_id,
                    utc_offset_hours=off,
                )
                if inserted:
                    stat["attachments"] += 1

    # 重排 seq：Voyager 的顺序是权威的（它保留了轮次结构），
    # Takeout 独有、Voyager 导出范围之外的消息排在最后。
    seen = set(ordered_ids)
    leftovers = [
        int(r["id"]) for r in con.execute(
            "SELECT id FROM messages WHERE conversation_id = ? ORDER BY seq ASC",
            (conv_id,),
        ).fetchall() if int(r["id"]) not in seen
    ]
    final = ordered_ids + leftovers
    if final:
        # 先用 -id 占位，避免 UNIQUE(conversation_id, seq) 冲突
        con.execute("UPDATE messages SET seq = -id WHERE conversation_id = ?", (conv_id,))
        for i, mid in enumerate(final, 1):
            con.execute("UPDATE messages SET seq = ? WHERE id = ?", (i, mid))
    stat["total_messages"] = len(final)

    if verbose:
        when = clock.local_str(created_at, off) if created_at else "未知（精度 unknown）"
        print(f"导入 Voyager 对话：{title}")
        print(f"  会话 id={conv_id}  时间={when}  {stat['messages']} 条新消息  "
              f"{stat['attachments']} 个附件")
        if stat["skipped_existing"]:
            print(f"  复用已存在的 user 消息 {stat['skipped_existing']} 条")
    return stat
