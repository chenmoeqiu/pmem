"""检索层：双索引。

索引一（主路径）：标签 —— 从 memory_tags 命中，解决"同义词/词汇鸿沟"
索引二（兜底）：原文 —— FTS5 精确匹配，解决"型号/金额/编号/人名"

本模块第一批只启用原文索引；标签索引的写入在第三批（打标）实现，
但查询接口已经留好（search_by_tag）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import datetime as _dt

from . import clock, text
from .config import Config


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


def muted_conversation_ids(con: sqlite3.Connection, mention: str | None) -> list[int]:
    """默认不调用的会话：当前这句话没点到它的触发词，就不进检索结果。

    触发词必须出自**用户当前这句话**（与路由的关键词规则同源）。不能从"最近对话"
    里搬词——那正是要避免的过度联想。

    没配触发词的会话 = 永不召回（宁可没有，也不假装有）。想临时看到它，
    用 `pmem show` / `pmem search --tag`（按标签查是显式行为，不在这里过滤）。
    """
    out: list[int] = []
    low = (mention or "").lower()
    for r in con.execute(
            "SELECT id, recall_triggers FROM conversations WHERE COALESCE(no_recall, 0) = 1"):
        raw = (r["recall_triggers"] or "")
        trigs = [t.strip().lower() for t in raw.split(",") if t.strip()]
        if not trigs or not any(t in low for t in trigs):
            out.append(int(r["id"]))
    return out


def _window_bounds(window: str) -> tuple[int, int] | None:
    """解析 "01:00-08:00" → (开始分钟, 结束分钟)。支持跨午夜（23:00-07:00）。"""
    try:
        s, e = (window or "").split("-")
        sh, sm = [int(x) for x in s.strip().split(":")]
        eh, em = [int(x) for x in e.strip().split(":")]
    except Exception:
        return None
    if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
        return None
    return sh * 60 + sm, eh * 60 + em


def _overlap_hours(a: _dt.datetime, b: _dt.datetime, window: str,
                   off: float) -> float:
    """[a,b] 与每日睡眠窗口的重叠小时数（按本地时区算）。

    为什么要窗口而不是"跨天"：用户的作息是熬夜的。
    `22:03 → 次日 00:44` 跨了天但只隔 2.7 小时（没睡）；
    `00:59 → 同日 12:33` 没跨天但隔了 11.5 小时（**确实睡了一觉**）。
    用固定的"跨自然日"判断，后者会被错判成同一段——实测踩过。
    """
    bounds = _window_bounds(window)
    if bounds is None:
        return 0.0
    start_min, end_min = bounds
    tz = _dt.timezone(_dt.timedelta(hours=off))
    al, bl = a.astimezone(tz), b.astimezone(tz)
    total = 0.0
    day = al.date() - _dt.timedelta(days=1)
    while day <= bl.date() + _dt.timedelta(days=1):
        ws = _dt.datetime.combine(day, _dt.time(hour=start_min // 60,
                                               minute=start_min % 60), tzinfo=tz)
        we = _dt.datetime.combine(day, _dt.time(hour=end_min // 60,
                                               minute=end_min % 60), tzinfo=tz)
        if we <= ws:                     # 跨午夜的窗口
            we += _dt.timedelta(days=1)
        lo, hi = max(al, ws), min(bl, we)
        if hi > lo:
            total += (hi - lo).total_seconds() / 3600
        day += _dt.timedelta(days=1)
    return total


def _is_sleep_gap(newer: str, older: str, gap_hours: float, off: float,
                  sleep_window: str | None = None,
                  min_overlap: float = 2.0) -> bool:
    """两次消息之间是否"睡了一觉"。

    规则（用户 2026-11-06 修正后）：
    - 间隔必须 ≥ `gap_hours`
    - **并且**这段时间必须与「睡眠窗口」重叠 ≥ `min_overlap` 小时

    这取代了原来的"跨自然日 AND 间隔"——那个规则会把
    "熬夜到 00:44、中午 12:33 再聊"错判成同一段（没跨天），
    而人其实睡过了。睡眠窗口是比"自然日"更准的代理。

    没配 `sleep_window` 时退回旧规则（跨天 AND 间隔），保持向后兼容。
    """
    try:
        a = _dt.datetime.fromisoformat(newer.replace("Z", "+00:00"))
        b = _dt.datetime.fromisoformat(older.replace("Z", "+00:00"))
    except Exception:
        return False
    if a.tzinfo is None:
        a = a.replace(tzinfo=_dt.timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=_dt.timezone.utc)
    if (a - b).total_seconds() < gap_hours * 3600:
        return False
    if sleep_window:
        return _overlap_hours(b, a, sleep_window, off) >= min_overlap
    tz = _dt.timezone(_dt.timedelta(hours=off))
    return a.astimezone(tz).date() > b.astimezone(tz).date()


def segment_start_iso(con: sqlite3.Connection, cfg: Config, *,
                      gap_hours: float | None = None,
                      max_lookback: int = 600) -> str | None:
    """当前「清醒段」的起点。找不到边界返回 None（调用方退回按小时算）。

    这个值同时用于**热记忆注入**和**冷检索排除**——两者必须同源，否则会出现
    "既没注入又查不到"的空洞（那正是之前的 bug）。
    """
    off = float(cfg.get("runtime", "utc_offset_hours", default=8))
    gap = float(gap_hours if gap_hours is not None
                else cfg.get("chat", "sleep_gap_hours", default=6))
    window = cfg.get("chat", "sleep_window", default="01:00-08:00")
    min_overlap = float(cfg.get("chat", "sleep_min_overlap_hours", default=2))
    rows = con.execute(
        "SELECT created_at FROM messages "
        " WHERE state = 'active' AND created_at IS NOT NULL "
        " ORDER BY created_at DESC LIMIT ?", (max_lookback,)).fetchall()
    prev = None
    found = None
    for r in rows:
        cur = r["created_at"]
        if prev is not None and _is_sleep_gap(prev, cur, gap, off, window, min_overlap):
            found = prev          # 边界之后（含）就是当前这段
            break
        prev = cur
    # 手动补的睡眠点优先（用户 2026-11-06：作息不一样时给一个手动出口）
    mark = con.execute("SELECT MAX(at) AS m FROM sleep_marks").fetchone()
    mark_at = mark["m"] if mark else None
    if mark_at and (found is None or mark_at > found):
        return mark_at
    return found


def merge_hits(primary: list[dict], extra: list[dict], limit: int) -> list[dict]:
    """合并两路检索结果（标签 + 全文），按消息 id 去重、取较高分。

    标签是主路径（换说法也能到），全文是兜底（救型号/编号/人名）。
    """
    by_id: dict[int, dict] = {}
    seen_tag: dict[int, str] = {}
    for h in primary + extra:
        mid = h.get("id")
        if mid is None:
            continue
        old = by_id.get(mid)
        if old is None or (h.get("score") or 0) > (old.get("score") or 0):
            if old is not None and old.get("matched_keyword", "").startswith("#"):
                h.setdefault("matched_keyword", old["matched_keyword"])
            by_id[mid] = h
    out = sorted(by_id.values(), key=lambda r: (r.get("score") or 0), reverse=True)
    return out[:limit]


def search(
    con: sqlite3.Connection,
    cfg: Config,
    query: str,
    *,
    limit: int | None = None,
    conversation_id: int | None = None,
    exclude_conversation: int | None = None,
    mention: str | None = None,
    exclude_since: str | None = None,
    weight=None,
) -> list[dict[str, Any]]:
    """原文精确检索 + 时间衰减重排。

    宽召回 -> Python 侧重排，因为 FTS5 内部无法做
    `相关度 × 时间衰减 × 重要性` 这种复合打分。

    `weight` 是给"记忆曲线 / 情绪权重"留的钩子：`weight(row) -> float`，
    默认 None（恒等于 1，行为不变）。等有真实的检索反馈数据再往里填，
    现在不填任何权重——没有可校准的数据时，任何权重都是拍脑袋。
    """
    match = text.build_match(query)
    if not match:
        return []

    off = cfg.get("runtime", "utc_offset_hours", default=8)
    recall = int(cfg.get("search", "recall", default=200))
    limit = int(limit or cfg.get("search", "default_limit", default=10))
    half_life = float(cfg.get("search", "half_life_days", default=180))

    sql = """
        SELECT m.id, m.conversation_id, m.role, m.origin, m.content,
               m.created_at, m.time_precision,
               c.title AS conversation_title, c.source_id, c.url,
               -bm25(messages_fts) AS relevance
          FROM messages_fts
          JOIN messages m      ON m.id = messages_fts.rowid
          JOIN conversations c ON c.id = m.conversation_id
         WHERE messages_fts MATCH ?
           AND m.state = 'active'
    """
    params: list[Any] = [match]
    if conversation_id is not None:
        sql += " AND m.conversation_id = ?"
        params.append(conversation_id)
    if exclude_conversation is not None:
        # 必须在 SQL 层排除，不能取完再过滤：否则召回名额会被要丢弃的行占满，
        # 真实历史反而被挤出去（实测过：当前对话把 top-8 全占了）
        sql += " AND m.conversation_id <> ?"
        params.append(exclude_conversation)
    muted = muted_conversation_ids(con, mention if mention is not None else query)
    if muted:
        # 同样必须在 SQL 层排除：事后过滤会被这些会话占满召回名额
        sql += " AND m.conversation_id NOT IN (%s)" % ",".join("?" * len(muted))
        params.extend(muted)
    if exclude_since:
        # 热记忆已经注入的那一段不再重复检索；**边界之外必须可查**。
        # 时间未知（created_at 为 NULL）的消息不属于任何热窗，保留可检索。
        sql += " AND (m.created_at IS NULL OR m.created_at < ?)"
        params.append(exclude_since)
    sql += " ORDER BY relevance DESC LIMIT ?"
    params.append(recall)

    rows = [_row_to_dict(r) for r in con.execute(sql, params).fetchall()]

    for r in rows:
        age = clock.age_days(r["created_at"])
        r["age_days"] = age
        r["decay"] = clock.decay(age, half_life)
        r["weight"] = float(weight(r)) if weight else 1.0
        r["score"] = r["relevance"] * r["decay"] * r["weight"]
        r["created_at_local"] = clock.local_str(r["created_at"], off)
        r["snippet"] = text.make_snippet(r["content"], query)

    rows.sort(key=lambda r: (r["score"], r["relevance"]), reverse=True)
    return rows[:limit]


def search_multi(
    con: sqlite3.Connection,
    cfg: Config,
    keywords: list[str],
    *,
    limit: int | None = None,
    exclude_conversation: int | None = None,
    mention: str | None = None,
    exclude_since: str | None = None,
) -> list[dict[str, Any]]:
    """多关键词召回：逐个查、按消息 id 合并、取最高分。

    用 OR 语义而不是把所有词 AND 在一起——多词 AND 很容易一条都不剩，
    宁可先召回再让排序说话。

    `mention` 是用户当前这句话，用来判断"默认不调用的会话"是否被点到。
    `exclude_since` 是热记忆边界：这一段已经注入，不再重复检索。
    """
    if not keywords:
        return []
    limit = int(limit or cfg.get("search", "default_limit", default=10))
    per_kw = max(limit * 6, 30)
    merged: dict[int, dict[str, Any]] = {}
    for kw in keywords:
        for r in search(con, cfg, kw, limit=per_kw,
                        exclude_conversation=exclude_conversation,
                        mention=mention, exclude_since=exclude_since):
            prev = merged.get(r["id"])
            if prev is None or r["score"] > prev["score"]:
                r["matched_keyword"] = kw
                merged[r["id"]] = r
    out = sorted(merged.values(), key=lambda r: r["score"], reverse=True)
    return out[:limit]


def search_by_tag(
    con: sqlite3.Connection, cfg: Config, tag: str, *,
    dimension: str | None = None, limit: int = 10,
    include_descendants: bool = True,
    exclude_since: str | None = None,
    mention: str | None = None,
    weight=None,
) -> list[dict[str, Any]]:
    """按标签召回：标签挂在话题段上，段映射回消息区间。

    **查父含子**：`tags.path` 存的是 `地点/中国/甲省` 这种物化路径，
    所以"中国的全部地点"就是一次前缀匹配，不需要手工维护子集，也不需要递归。
    默认开启（is-a 的意义就在这里），`include_descendants=False` 可关掉。

    **只认已批准的值**——待审的值能看见，但不能用来检索。
    """
    off = cfg.get("runtime", "utc_offset_hours", default=8)
    base = con.execute(
        "SELECT id, path, dimension FROM tags WHERE name = ?", (tag,)
    ).fetchone()
    if not base:
        return []

    prefix = base["path"] or ""
    sql = """
        SELECT m.id, m.conversation_id, m.role, m.content, m.created_at,
               m.time_precision, c.title AS conversation_title, c.source_id,
               t.name AS tag, t.dimension AS dim, t.path AS tag_path,
               s.title AS segment_title, s.start_seq, s.end_seq
          FROM segment_tags st
          JOIN tags t      ON t.id = st.tag_id
          JOIN segments s  ON s.id = st.segment_id
          JOIN messages m  ON m.conversation_id = s.conversation_id
                          AND m.seq BETWEEN s.start_seq AND s.end_seq
          JOIN conversations c ON c.id = m.conversation_id
         WHERE t.state = 'approved' AND m.state = 'active'
    """
    params: list[Any] = []
    if include_descendants and prefix:
        sql += " AND (t.path = ? OR t.path LIKE ?)"
        params += [prefix, prefix + "/%"]
    else:
        sql += " AND t.name = ?"
        params.append(tag)
    if dimension:
        sql += " AND t.dimension = ?"
        params.append(dimension)
    if exclude_since:
        sql += " AND (m.created_at IS NULL OR m.created_at < ?)"
        params.append(exclude_since)
    muted = muted_conversation_ids(con, mention) if mention is not None else []
    if muted:
        sql += " AND m.conversation_id NOT IN (%s)" % ",".join("?" * len(muted))
        params.extend(muted)
    sql += " ORDER BY m.created_at DESC LIMIT ?"
    params.append(limit)

    rows = con.execute(sql, params).fetchall()
    out = []
    half_life = float(cfg.get("search", "half_life_days", default=180))
    for r in rows:
        d = _row_to_dict(r)
        d["created_at_local"] = clock.local_str(d["created_at"], off)
        d["snippet"] = text.make_snippet(d["content"], tag)
        # 标签命中给统一相关度，再按时间衰减排——与全文结果的 score 同一量纲，
        # 这样 merge_hits 才能把两路结果放在一起排序。
        age = clock.age_days(d["created_at"])
        d["age_days"] = age
        d["relevance"] = 1.0
        d["decay"] = clock.decay(age, half_life)
        d["weight"] = float(weight(d)) if weight else 1.0
        d["score"] = d["decay"] * d["weight"]
        d["matched_keyword"] = "#" + tag
        out.append(d)
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def hot_window(con: sqlite3.Connection, cfg: Config, *, hours: int = 24,
               mention: str | None = None,
               include_muted: bool = False,
               since: str | None = None) -> list[dict[str, Any]]:
    """取当前「清醒段」的消息（热记忆：整段注入，不检索）。

    `since` 优先：它是 `segment_start_iso()` 算出的段起点，**与冷检索排除用同一个值**。
    没有 `since` 时退回"最近 N 小时"（旧行为，仅在无法判断边界时使用）。

    **默认排除"默认不调用"的会话**——热窗不检索，但注入同样是"被调用"。
    `include_muted=True` 只给 `pmem hot` 这类显式查看用。
    """
    off = cfg.get("runtime", "utc_offset_hours", default=8)
    start = since or clock.window_start_iso(hours)
    sql = """
        SELECT m.id, m.conversation_id, m.role, m.origin, m.content, m.created_at,
               m.time_precision, c.title AS conversation_title,
               (SELECT COUNT(*) FROM attachments a
                 WHERE a.message_id = m.id AND a.state = 'active') AS att_n,
               (SELECT a.description FROM attachments a
                 WHERE a.message_id = m.id AND a.state = 'active'
                   AND a.description IS NOT NULL AND a.description <> ''
                 LIMIT 1) AS att_desc
          FROM messages m
          JOIN conversations c ON c.id = m.conversation_id
         WHERE m.state = 'active'
           AND m.created_at IS NOT NULL
           AND m.created_at >= ?
           AND c.source <> 'task'
    """
    params: list[Any] = [start]
    if not include_muted:
        muted = muted_conversation_ids(con, mention)
        if muted:
            sql += " AND m.conversation_id NOT IN (%s)" % ",".join("?" * len(muted))
            params.extend(muted)
    sql += " ORDER BY m.created_at ASC"
    rows = con.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["created_at_local"] = clock.local_str(d["created_at"], off)
        out.append(d)
    return out


def get_message(con: sqlite3.Connection, cfg: Config, message_id: int) -> dict[str, Any] | None:
    off = cfg.get("runtime", "utc_offset_hours", default=8)
    row = con.execute(
        """
        SELECT m.*, c.title AS conversation_title, c.url, c.source_id
          FROM messages m
          JOIN conversations c ON c.id = m.conversation_id
         WHERE m.id = ?
        """,
        (message_id,),
    ).fetchone()
    if not row:
        return None
    d = _row_to_dict(row)
    d["created_at_local"] = clock.local_str(d["created_at"], off)
    d.pop("content_indexed", None)
    atts = con.execute(
        "SELECT id, kind, orig_name, sha256, path, bytes, mime FROM attachments "
        "WHERE message_id = ? AND state = 'active'",
        (message_id,),
    ).fetchall()
    d["attachments"] = [_row_to_dict(a) for a in atts]
    return d


def stats(con: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    off = cfg.get("runtime", "utc_offset_hours", default=8)
    q = lambda sql, *p: con.execute(sql, p).fetchone()[0]  # noqa: E731
    first = q("SELECT MIN(created_at) FROM messages WHERE created_at IS NOT NULL")
    last = q("SELECT MAX(created_at) FROM messages WHERE created_at IS NOT NULL")
    by_prec = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT time_precision, COUNT(*) FROM messages GROUP BY time_precision"
        ).fetchall()
    }
    return {
        "conversations": q("SELECT COUNT(*) FROM conversations WHERE state='active'"),
        "messages": q("SELECT COUNT(*) FROM messages WHERE state='active'"),
        "by_role": {
            r[0]: r[1]
            for r in con.execute(
                "SELECT role, COUNT(*) FROM messages WHERE state='active' GROUP BY role"
            ).fetchall()
        },
        "by_time_precision": by_prec,
        "attachments": q("SELECT COUNT(*) FROM attachments WHERE state='active'"),
        "attachment_mb": round(
            q("SELECT COALESCE(SUM(bytes),0) FROM attachments WHERE state='active'") / 1048576, 1
        ),
        "first_message_local": clock.local_str(first, off) if first else None,
        "last_message_local": clock.local_str(last, off) if last else None,
        "purged_messages": q("SELECT COUNT(*) FROM messages WHERE state='purged'"),
    }
