"""打标：多维标签 + is-a 层级 + 话题段。

三条纪律（docs/DESIGN.md 第 9 节"打标"）：
1. **闭集**：模型只能从已批准的词表里选；自己编的值进待审。
2. **待审不参与检索**：新值落 state='pending'，批准后立刻生效（过滤的是状态）。
3. **标签挂在"话题段"上，不挂在消息上**：标签描述"这一段在讲什么"。

为什么不复制原文来拆话题：
原文复制会变成两份，purge 穿透、导出一致性、话题交织全都要额外处理。
所以用 `segments(conversation_id, start_seq, end_seq)` 记区间——就是"子文件"，
但不复制内容。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import clock, prompt

DIMS = prompt.DIMENSIONS

# 起步词表：每维给几个最通用的值，剩下的靠 propose 从真实数据里长
SEED_VOCABULARY: list[tuple[str, str, str | None]] = [
    ("主题", "工作", None),
    ("主题", "投资", None),
    ("主题", "旅行", None),
    ("主题", "学习", None),
    ("主题", "创作", None),
    ("主题", "生活", None),
    ("任务", "规划", None),
    ("任务", "决策", None),
    ("任务", "查询", None),
    ("任务", "评估", None),
    ("地点", "中国", None),
    ("情绪", "焦虑", None),
    ("情绪", "疲惫", None),
]


def seed(con: sqlite3.Connection) -> int:
    n = 0
    for dim, name, parent in SEED_VOCABULARY:
        if con.execute("SELECT 1 FROM tags WHERE name = ?", (name,)).fetchone():
            continue
        add_tag(con, name, dimension=dim, parent=parent, state="approved")
        n += 1
    return n


def _tag_by_name(con: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT id, name, dimension, state, parent_id, path FROM tags WHERE name = ?",
        (name,),
    ).fetchone()


def add_tag(
    con: sqlite3.Connection,
    name: str,
    *,
    dimension: str = "主题",
    parent: str | None = None,
    state: str = "approved",
) -> dict:
    name = (name or "").strip()
    if not (2 <= len(name) <= 30):
        raise ValueError("标签名要在 2-30 字之间")
    if dimension not in DIMS:
        raise ValueError(f"dimension 必须是 {DIMS}")

    row = _tag_by_name(con, name)
    if row:
        return {"ok": True, "id": row["id"], "name": name, "dimension": row["dimension"],
                "state": row["state"], "existed": True}

    parent_id = None
    path = f"{dimension}/{name}"
    if parent:
        prow = _tag_by_name(con, parent)
        if prow:
            parent_id = int(prow["id"])
            path = f"{prow['path']}/{name}"
    con.execute(
        "INSERT INTO tags(name, kind, state, dimension, parent_id, path, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, dimension, state, dimension, parent_id, path, clock.now_utc_iso()),
    )
    tid = int(con.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    return {"ok": True, "id": tid, "name": name, "dimension": dimension,
            "state": state, "existed": False}


def vocabulary(con: sqlite3.Connection, state: str = "approved") -> list[dict]:
    return [
        {"id": r["id"], "name": r["name"], "dimension": r["dimension"],
         "state": r["state"], "parent_id": r["parent_id"], "path": r["path"]}
        for r in con.execute(
            "SELECT * FROM tags WHERE state = ? ORDER BY dimension, id", (state,))
    ]


def _conversation_messages(con: sqlite3.Connection, cid: int) -> list[dict]:
    return [
        {"seq": r["seq"], "role": r["role"], "content": r["content"]}
        for r in con.execute(
            "SELECT seq, role, content FROM messages WHERE conversation_id = ? "
            "AND state = 'active' ORDER BY seq", (cid,))
    ]


def apply_segments(
    con: sqlite3.Connection,
    conversation_id: int,
    messages: list[dict],
    data: dict,
) -> dict[str, Any]:
    """把"切段 + 打标"的结果落到派生层。原文不动。"""
    stat = {"segments": 0, "tags": 0, "new_pending": 0, "invalid": 0}
    valid_seqs = {m["seq"] for m in messages}
    approved = {(t["dimension"], t["name"]): t for t in vocabulary(con)}

    for seg in (data.get("segments") or []):
        if not isinstance(seg, dict):
            continue
        try:
            lo, hi = int(seg.get("from")), int(seg.get("to"))
        except (TypeError, ValueError):
            continue
        if lo > hi:
            lo, hi = hi, lo
        if lo not in valid_seqs:
            continue
        hi = min(hi, max(valid_seqs))
        cur = con.execute(
            "INSERT INTO segments(conversation_id, start_seq, end_seq, title, reason, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (conversation_id, lo, hi, str(seg.get("title") or "")[:60],
             str(seg.get("reason") or "")[:120], clock.now_utc_iso()),
        )
        seg_id = int(cur.lastrowid)
        stat["segments"] += 1

        for t in (seg.get("tags") or [])[:6]:
            if not isinstance(t, dict):
                continue
            dim = str(t.get("dim") or "").strip()
            name = str(t.get("name") or "").strip()
            if dim not in DIMS or not name:
                stat["invalid"] += 1
                continue
            # add_tag 按设计对长度不合法的名字抛 ValueError；这里先挡住，
            # 否则模型偶尔吐一个 1 字标签就会炸掉整批（实测卡死过一次）。
            if not (2 <= len(name) <= 30):
                stat["invalid"] += 1
                continue
            key = (dim, name)
            if key in approved:
                tid = int(approved[key]["id"])
            else:
                if con.execute("SELECT 1 FROM tags WHERE name = ?", (name,)).fetchone():
                    stat["invalid"] += 1  # 同名但不同维度，不猜
                    continue
                info = add_tag(con, name, dimension=dim,
                               parent=str(t.get("parent") or "").strip() or None,
                               state="pending")
                tid = int(info["id"])
                approved[key] = {"id": tid, "name": name, "dimension": dim}
                stat["new_pending"] += 1
            con.execute(
                "INSERT INTO segment_tags(segment_id, tag_id, weight, source, created_at) "
                "VALUES (?,?,1.0,'agent',?) ON CONFLICT(segment_id, tag_id) DO NOTHING",
                (seg_id, tid, clock.now_utc_iso()),
            )
            stat["tags"] += 1
    return stat


def untagged_conversations(con: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    rows = con.execute(
        """SELECT c.id, c.title FROM conversations c
            WHERE c.state = 'active'
              AND NOT EXISTS (SELECT 1 FROM segments s WHERE s.conversation_id = c.id)
            ORDER BY c.started_at DESC LIMIT ?""", (limit,),
    ).fetchall()
    return [{"id": r["id"], "title": r["title"] or ""} for r in rows]


def run(
    con: sqlite3.Connection, cfg, provider, *,
    limit: int = 10, dry: bool = False,
) -> dict[str, Any]:
    """按会话打标：切段 + 多维标签。只写派生层。"""
    if not vocabulary(con):
        seed(con)
    vocab = vocabulary(con)
    todo = untagged_conversations(con, limit=limit)

    total = {"conversations": 0, "segments": 0, "tags": 0,
             "new_pending": 0, "invalid": 0, "failed": 0}
    for c in todo:
        msgs = _conversation_messages(con, c["id"])
        if not msgs:
            continue
        if dry:
            total["conversations"] += 1
            continue
        try:
            raw, _u = provider.tag_batch(
                prompt.build_segment_messages(c["title"], msgs, vocab), max_tokens=2500)
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            st = apply_segments(con, c["id"], msgs, data)
        except Exception:
            # 单场失败不能炸掉整批：失败计数后跳过，下一轮会重新排队。
            total["failed"] += 1
            continue
        total["conversations"] += 1
        for k in ("segments", "tags", "new_pending", "invalid"):
            total[k] += st[k]
    total["dry"] = dry
    return total


def mark_cold_before(con: sqlite3.Connection, boundary_iso: str) -> int:
    """把边界之前的会话标为「待整理」。返回新入队数量。

    边界 = `search.segment_start_iso()` 算出的清醒段起点。睡了一觉之后，
    上一段就转冷了——但**整理不在这一刻做**，只入队，等闲时（谷价时段）批量跑。
    只收**还没打过标**的会话：已整理过的不再排一次，避免重复烧调用。
    """
    rows = con.execute(
        """SELECT c.id FROM conversations c
            WHERE c.state = 'active'
              AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
              AND NOT EXISTS (SELECT 1 FROM messages m2 WHERE m2.conversation_id = c.id
                               AND (m2.created_at IS NULL OR m2.created_at >= ?))
              AND NOT EXISTS (SELECT 1 FROM segments s WHERE s.conversation_id = c.id)
              AND NOT EXISTS (SELECT 1 FROM cold_queue q WHERE q.conversation_id = c.id)""",
        (boundary_iso,)).fetchall()
    for r in rows:
        con.execute("INSERT OR IGNORE INTO cold_queue(conversation_id, marked_at) VALUES (?,?)",
                    (int(r["id"]), clock.now_utc_iso()))
    return len(rows)


def pending_cold(con: sqlite3.Connection, limit: int = 50) -> list[int]:
    """待整理队列里的会话 id（老的先处理）。"""
    return [int(r[0]) for r in con.execute(
        "SELECT conversation_id FROM cold_queue WHERE done_at IS NULL "
        "ORDER BY id LIMIT ?", (limit,))]


def run_pending(con: sqlite3.Connection, cfg, provider, *, limit: int = 50) -> dict:
    """把待整理队列跑掉：逐场切段 + 打标，完成后标记 done。闲时调用。"""
    ids = pending_cold(con, limit)
    total = {"queued": len(ids), "conversations": 0, "segments": 0,
             "tags": 0, "new_pending": 0, "invalid": 0, "failed": 0}
    for cid in ids:
        r = rerun_one(con, provider, cid)
        if r.get("ok"):
            total["conversations"] += 1
            for k in ("segments", "tags", "new_pending", "invalid"):
                total[k] += int(r.get(k, 0) or 0)
            con.execute("UPDATE cold_queue SET done_at = ? WHERE conversation_id = ?",
                        (clock.now_utc_iso(), cid))
        else:
            total["failed"] += 1
    return total


def forget(con: sqlite3.Connection, conversation_id: int) -> int:
    """丢掉一场会话的派生标签（段 + 关联），让它重新入队。原文一个字不动。"""
    n = int(con.execute("SELECT count(*) FROM segments WHERE conversation_id = ?",
                        (conversation_id,)).fetchone()[0])
    con.execute("DELETE FROM segments WHERE conversation_id = ?", (conversation_id,))
    con.execute("UPDATE messages SET tagged_at = NULL WHERE conversation_id = ?",
                (conversation_id,))
    return n


def forget_all(con: sqlite3.Connection) -> int:
    """清空全部派生标签。用于提示词改了之后整体重打。"""
    n = int(con.execute("SELECT count(*) FROM segments").fetchone()[0])
    con.execute("DELETE FROM segments")
    con.execute("UPDATE messages SET tagged_at = NULL WHERE tagged_at IS NOT NULL")
    return n


def rerun_one(con: sqlite3.Connection, provider, conversation_id: int) -> dict:
    """重打一场会话：先丢掉旧段，再按**当前**提示词重打。"""
    row = con.execute("SELECT title FROM conversations WHERE id = ?",
                      (conversation_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "没有这场会话"}
    if not vocabulary(con):
        seed(con)
    vocab = vocabulary(con)
    msgs = _conversation_messages(con, conversation_id)
    if not msgs:
        return {"ok": False, "reason": "这场没有消息"}
    dropped = forget(con, conversation_id)
    try:
        raw, _u = provider.tag_batch(
            prompt.build_segment_messages(row["title"] or "", msgs, vocab),
            max_tokens=2500)
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        st = apply_segments(con, conversation_id, msgs, data)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e), "dropped": dropped}
    return {"ok": True, "dropped": dropped, **st}


def propose(
    con: sqlite3.Connection, cfg, provider, *, limit: int = 200, batch_size: int = 20,
) -> dict[str, Any]:
    """只提议、不打标：从真实消息里归纳"维度 + 值"。不写库。"""
    rows = con.execute(
        "SELECT id, content FROM messages WHERE state = 'active' AND role = 'user' "
        "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    todo = [{"id": r["id"], "content": r["content"]} for r in rows]
    if not todo:
        return {"scanned": 0, "batches": 0, "candidates": [], "raw_count": 0}

    agg: dict[tuple[str, str], dict[str, Any]] = {}
    batches = 0
    for start in range(0, len(todo), batch_size):
        chunk = todo[start:start + batch_size]
        batches += 1
        try:
            raw, _u = provider.tag_batch(
                prompt.build_propose_messages(chunk), max_tokens=1500)
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            continue
        for it in (data.get("items") or [])[:16]:
            if not isinstance(it, dict):
                continue
            dim = str(it.get("dim") or "").strip()
            name = str(it.get("name") or "").strip()
            if dim not in DIMS or not (2 <= len(name) <= 8):
                continue
            key = (dim, name)
            item = agg.setdefault(key, {"dim": dim, "name": name,
                                        "parent": str(it.get("parent") or "").strip(),
                                        "count": 0})
            item["count"] += 1

    existing = {r["name"] for r in con.execute("SELECT name FROM tags")}
    cands = [v for v in agg.values() if v["name"] not in existing]
    cands.sort(key=lambda x: (DIMS.index(x["dim"]), -x["count"], x["name"]))
    return {"scanned": len(todo), "batches": batches,
            "candidates": cands, "raw_count": len(cands)}


def consolidate(provider, candidates: list[dict]) -> list[dict]:
    """同维度内合并同义名。**每一轮都分块**，直到收敛或不再减少。

    原实现只对第一轮分块，第二轮把 staged（数百个）一次性塞进 max_tokens=4000 的调用，
    必然截断 → JSON 解析失败 → 静默返回原样（表现为"合并后 315 个"，等于没合并）。
    """
    if len(candidates) < 6:
        return candidates
    staged = list(candidates)
    for _ in range(4):
        if len(staged) <= 40:
            break
        groups: list[list[dict]] = []
        for dim in DIMS:
            same = [c for c in staged if c["dim"] == dim]
            groups += [same[i:i + 60] for i in range(0, len(same), 60)]
        nxt: list[dict] = []
        for g in groups:
            nxt.extend(_merge_once(provider, g))
        # 没有变少就停：多半是这批调用失败了，别空转烧钱
        if len(nxt) >= len(staged):
            staged = nxt
            break
        staged = nxt
    staged.sort(key=lambda x: (DIMS.index(x["dim"]), -x["count"], x["name"]))
    return staged


def _merge_once(provider, candidates: list[dict]) -> list[dict]:
    try:
        raw, _u = provider.tag_batch(
            prompt.build_consolidate_messages(candidates), max_tokens=4000)
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return candidates
    by_key = {(c["dim"], c["name"]): c for c in candidates}
    out: list[dict] = []
    used: set[tuple[str, str]] = set()
    for cat in (data.get("categories") or []):
        if not isinstance(cat, dict):
            continue
        dim = str(cat.get("dim") or "").strip()
        name = str(cat.get("name") or "").strip()
        if dim not in DIMS or not (2 <= len(name) <= 8):
            continue
        members = [m for m in (cat.get("members") or []) if isinstance(m, str)]
        hits = [by_key[(dim, m)] for m in members if (dim, m) in by_key]
        if not hits:
            continue
        used.update((dim, m) for m in members if (dim, m) in by_key)
        out.append({"dim": dim, "name": name,
                    "parent": str(cat.get("parent") or "").strip(),
                    "count": sum(h["count"] for h in hits),
                    "members": [h["name"] for h in hits]})
    for c in candidates:
        if (c["dim"], c["name"]) not in used:
            out.append({**c, "members": [c["name"]]})
    return out


def list_tags(con: sqlite3.Connection, *, state: str = "all", limit: int = 300) -> list[dict]:
    sql = """SELECT t.id, t.name, t.dimension, t.state, t.path, p.name AS parent,
                    (SELECT COUNT(*) FROM segment_tags st WHERE st.tag_id = t.id) AS uses
               FROM tags t LEFT JOIN tags p ON p.id = t.parent_id"""
    params: list[Any] = []
    if state != "all":
        sql += " WHERE t.state = ?"
        params.append(state)
    sql += " ORDER BY t.dimension, uses DESC, t.id LIMIT ?"
    params.append(limit)
    return [{k: r[k] for k in r.keys()} for r in con.execute(sql, params)]


def set_state(con: sqlite3.Connection, tag_id: int, state: str) -> dict:
    row = con.execute("SELECT id, name FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": f"没有标签 #{tag_id}"}
    con.execute("UPDATE tags SET state = ? WHERE id = ?", (state, tag_id))
    return {"ok": True, "id": tag_id, "name": row["name"], "state": state}
