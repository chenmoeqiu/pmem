"""对外接口层：给运行时（DSH 或任何 agent 程序）用的两个出口。

设计原则（docs/DESIGN.md 第 12 节"接入"）：
- **上下文的决定权 100% 在 pmem**。运行时不许自己拼上下文、不许有自己的历史来源，
  只能拿 `build_context()` 返回的这一份。
- 时间戳只由 pmem 盖章，`note()` 不接受调用方传入"现在几点"。
- 这两个函数不生成任何东西，也不做任何决策以外的推断——生成是运行时的事。

为什么不做成"可插拔的抽象接口"：因为这两条命令就是最小公约数。
任何程序会调命令行，就已经是合法接入方，不需要预留插槽。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import clock, gate, prompt, route, search, store
from .config import Config


def _live_conversation(con: sqlite3.Connection, off: float) -> int:
    stamp, _ = clock.local_stamp(None, off)
    day = stamp.split("-")[0]
    return store.upsert_conversation(
        con, source="live", source_id=f"live-{day}",
        title=f"实时对话 {day[:4]}-{day[4:6]}-{day[6:]}",
        started_at=clock.now_utc_iso(), time_precision="message",
    )


def _im_conversation(con: sqlite3.Connection, source: str, session: str) -> int:
    """IM 会话：按（来源, 外部会话 id）稳定落到同一场，跨天续聊也不断开。

    为什么不复用 `_live_conversation`：那是一天一场，IM 一天几百轮会把
    话题全挤进一场，切段和热窗都会失真。
    """
    src = (source or "im").strip() or "im"
    sid = f"im:{src}:{(session or 'default').strip()}"
    return store.upsert_conversation(
        con, source=src, source_id=sid,
        title=f"{src} 会话 {(session or 'default')[:12]}",
        started_at=clock.now_utc_iso(), time_precision="message")


def build_context(
    con: sqlite3.Connection,
    cfg: Config,
    provider,
    text: str,
    *,
    source: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """把"该带什么上下文"算出来，交给运行时去生成。**不写库、不生成。**

    `source` / `session` 给 IM 用：同一个外部会话稳定映射到同一场 pmem 会话，
    这样热窗的"最近对话"和 note 的落点是一致的。
    """
    off = float(cfg.get("runtime", "utc_offset_hours", default=8))
    live = (_im_conversation(con, source, session) if session
            else _live_conversation(con, off))

    recent_rows = con.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? AND state = 'active' "
        "ORDER BY seq DESC LIMIT 6", (live,),
    ).fetchall()
    recent = [{"role": r["role"], "content": r["content"]} for r in reversed(recent_rows)]

    # 热记忆边界 = 当前清醒段起点；注入与检索排除同源，保证"滑出即可检索"
    since = search.segment_start_iso(con, cfg)
    if since:
        from . import tagging as _tagging
        _tagging.mark_cold_before(con, since)
    else:
        since = clock.window_start_iso(int(cfg.get("chat", "hot_hours", default=24)))

    from . import tagging
    vocab = [t["name"] for t in tagging.vocabulary(con)]
    d = route.decide(provider, text, recent, vocab=vocab)

    hits: list[dict[str, Any]] = []
    if d.need_recall:
        limit = int(cfg.get("chat", "hits_limit", default=8))
        if d.keywords:
            hits = search.search_multi(
                con, cfg, d.keywords, limit=limit,
                # "默认不调用"的会话：只有用户当前这句话点到触发词才放行
                mention=text, exclude_since=since,
            )
        # 标签是主路径（换个说法也能到），全文只兜型号/编号/人名
        tag_hits: list[dict[str, Any]] = []
        for tg in (d.tags or [])[:4]:
            tag_hits += search.search_by_tag(
                con, cfg, tg, limit=max(3, limit // 2),
                mention=text, exclude_since=since)
        if tag_hits:
            hits = search.merge_hits(hits, tag_hits, limit)

    verdict = None
    gated = hits
    if hits and bool(cfg.get("chat", "gate_enabled", default=True)):
        try:
            vraw, _u = provider.assess(text, hits)
            verdict = gate.validate(vraw, len(hits))
            if verdict.sufficient:
                gated = [hits[i - 1] for i in verdict.used] or hits
            else:
                gated = []
        except Exception:
            verdict = None

    hot = search.hot_window(
        con, cfg, hours=int(cfg.get("chat", "hot_hours", default=24)),
        mention=text, since=since,
    )
    hot_text = "" if d.domain == "work" else prompt.render_hot(
        hot, int(cfg.get("chat", "hot_max_tokens", default=0))
    )
    hits_text = prompt.render_hits(
        gated, int(cfg.get("chat", "hits_limit", default=8)),
        int(cfg.get("chat", "hit_max_tokens", default=300)),
    )
    if d.need_recall and d.keywords:
        from . import images as _images
        img_rows: list[dict[str, Any]] = []
        for kw in d.keywords[:3]:
            img_rows += _images.search(con, kw, limit=2)
        img_text = _images.render(img_rows)
        if img_text:
            hits_text = f"{hits_text}\n\n【相关图片】\n{img_text}".strip()

    messages = prompt.build_chat_messages(
        user_text=text, hot_text=hot_text, hits_text=hits_text,
        domain=d.domain, gated=verdict is not None and verdict.sufficient,
        now_line=prompt.current_time_line(off),
        persona=prompt.load_persona(cfg),
    )

    return {
        "messages": messages,
        "hot_text": hot_text,
        "hits_text": hits_text,
        "domain": d.domain,
        "need_recall": d.need_recall,
        "keywords": d.keywords,
        "tags": d.tags,
        "ambiguous": d.ambiguous,
        "retrieved": len(hits),
        "used": len(gated),
        "gate_sufficient": None if verdict is None else verdict.sufficient,
        "gate_missing": None if verdict is None else verdict.missing,
        "reason": d.reason,
    }


def note(
    con: sqlite3.Connection,
    cfg: Config,
    *,
    text: str,
    role: str = "user",
    conversation_id: int | None = None,
    source: str | None = None,
    session: str | None = None,
    attachments: list[str] | None = None,
    describe: bool = False,
    provider=None,
) -> dict[str, Any]:
    """写一条消息（可带附件）。**时间由 pmem 盖章**，调用方无权传入。

    `source` / `session`：IM 场景下把不同平台、不同外部会话分开落库，
    否则一天几百轮全挤进「实时对话 <日期>」一场。

    `attachments`：IM 收到的图片/文件路径。**这就补上了"IM 收图入口"**——
    图片按 sha256 落盘、挂到这条消息上，原文与附件都保留。
    `describe=True` 时顺便写一句话描述（每张一次视觉调用），默认关闭：
    描述可以事后用 `pmem describe` 补，不必在收图时阻塞。
    """
    off = float(cfg.get("runtime", "utc_offset_hours", default=8))
    if conversation_id:
        cid = conversation_id
    elif session:
        cid = _im_conversation(con, source or "im", session)
    else:
        cid = _live_conversation(con, off)
    mid = store.add_message(
        con, conversation_id=cid, role=role, origin=role,
        content=text, created_at=clock.now_utc_iso(),
        time_precision="message", source=(source or "live"),
    )
    out: dict[str, Any] = {"message_id": mid, "conversation_id": cid,
                           "created_at": clock.now_utc_iso()}

    if attachments:
        media = cfg.path("paths", "media")
        saved: list[dict[str, Any]] = []
        errors: list[str] = []
        for raw in attachments:
            p = Path(raw)
            if not p.exists() or not p.is_file():
                errors.append(f"文件不存在：{raw}")
                continue
            try:
                aid, rel, is_new = store.store_attachment(
                    con, media, data=p.read_bytes(), orig_name=p.name,
                    when_iso=None, message_id=mid, conversation_id=cid,
                    utc_offset_hours=off)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p.name} 存附件失败：{type(e).__name__}")
                continue
            saved.append({"attachment_id": aid, "path": rel, "new": bool(is_new)})
        out["attachments"] = saved
        out["attachment_errors"] = errors
        if describe and saved and provider is not None:
            from . import images as _images
            out["described"] = _images.describe_batch(
                con, cfg, provider,
                ids=[int(s["attachment_id"]) for s in saved])

    # 一轮结束（助手那条写完）顺手整理转冷的会话——"闲时"的默认触发点：
    # 调用方只管写，整理自己发生。队列空时只是一次 SQL，不影响这一轮。
    if role == "assistant":
        try:
            from . import daily as _daily
            out["daily"] = _daily.maybe_auto(con, cfg, provider)
        except Exception as e:  # noqa: BLE001
            out["daily"] = {"ok": False, "error": type(e).__name__}
    return out
