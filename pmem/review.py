"""复查：每周找矛盾，每月重新整理后对比。

两条原则（用户 2026-11-06 定）：

1. **只记录，不催改。** 发现疑点就写进 `pending_resolutions`，不改任何记忆、不催用户。
   用户有空时用 `pmem pending list` 看、按条回复（`pmem pending answer <id> "..."`）。
   这和"每日整理发现同义合并"是同一类人工接入点——**人工决定的时机由人定**。
2. **全部由用户决断。** 模型只负责"指出两个说法看起来冲突"，不做判定、
   不自动覆盖旧说法（那正是本项目的红线：不用新结论悄悄取代旧原文）。

weekly   —— 最近 N 天 vs 更早：同一话题下前后矛盾的说法（1 次模型调用）
monthly  —— 影子库里重打一遍标，与现有结果逐场对比，差异即为"漂移"（N 次调用）
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import clock, db, tagging

REVIEW_SYSTEM = """你在做记忆复查。给你两批说法：一批是**最近的**，一批是**更早的**。
两边都带标签。请只回答一个问题：**有没有前后矛盾的说法？**

判断标准（严格）：
- 必须针对**同一件事/同一个对象**（同一只股票、同一份工作、同一家店、同一个偏好）
- 必须是**实质冲突**：态度反转、事实不符、计划变了且明确否掉旧的
- **不算矛盾**：补充细节、程度变化、时间推进导致的状态变化（"准备买"→"买了"不是矛盾）、
  不同对象、不同场景

对每一条矛盾输出：
{"topic":"同一件事是什么","old":"更早的说法（原话片段）","new":"最近的说法（原话片段）",
 "why":"为什么冲突，一句话"}

没有矛盾就输出 {"contradictions":[]}。只输出 JSON，不要解释。"""


# ---------------------------------------------------------------- 存储


def _sig(kind: str, payload: dict) -> str:
    """去重签名。`sig_key` 优先（例如实体类按 entity_id 去重，
    否则"提到几段"一变就被当成新问题反复报）。"""
    if payload.get("sig_key"):
        return f"{kind}:{payload['sig_key']}"
    raw = json.dumps({k: payload.get(k) for k in ("topic", "old", "new")},
                     ensure_ascii=False, sort_keys=True)
    return f"{kind}:{raw}"


def add_pending(con: sqlite3.Connection, kind: str, payload: dict) -> int | None:
    """记一条待裁决。同一条（同 kind + 同 topic/old/new）只记一次。返回新 id 或 None。

    去重用**独立列 `sig`**，不能用 `payload LIKE`：sig 里嵌的是 JSON，
    存进去引号会被转义，LIKE 匹配不上（踩过）。被 dismissed 的也不再重复报——
    用户已经说过"这不是问题"，就别再催第二遍。
    """
    sig = _sig(kind, payload)
    if con.execute("SELECT id FROM pending_resolutions WHERE sig = ? LIMIT 1",
                   (sig,)).fetchone():
        return None
    payload = dict(payload, sig=sig)
    cur = con.execute(
        "INSERT INTO pending_resolutions(kind, payload, status, created_at, sig) "
        "VALUES (?,?, 'open', ?, ?)",
        (kind, json.dumps(payload, ensure_ascii=False), clock.now_utc_iso(), sig))
    return int(cur.lastrowid)


def list_pending(con: sqlite3.Connection, *, status: str = "open",
                 limit: int = 50) -> list[dict]:
    out = []
    for r in con.execute(
            "SELECT id, kind, payload, status, created_at, answer, answered_at, "
            "proposal, proposed_at, applied_at, apply_result "
            "FROM pending_resolutions WHERE status = ? ORDER BY id LIMIT ?",
            (status, limit)).fetchall():
        d = {k: r[k] for k in r.keys()}
        try:
            d["detail"] = json.loads(d["payload"] or "{}")
        except Exception:
            d["detail"] = {}
        out.append(d)
    return out


def answer(con: sqlite3.Connection, item_id: int, text: str) -> dict:
    """记下用户的描述。**不改状态**——除非用户明确 resolve。"""
    row = con.execute("SELECT id FROM pending_resolutions WHERE id = ?",
                      (item_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "没有这一条"}
    con.execute("UPDATE pending_resolutions SET answer = ?, answered_at = ? WHERE id = ?",
                (text, clock.now_utc_iso(), item_id))
    return {"ok": True, "id": item_id, "status": "open（回答已记录，仍未关闭）"}


def set_status(con: sqlite3.Connection, item_id: int, status: str) -> dict:
    if status not in ("resolved", "dismissed", "open"):
        return {"ok": False, "reason": "status 只能是 open/resolved/dismissed"}
    row = con.execute("SELECT id FROM pending_resolutions WHERE id = ?",
                      (item_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "没有这一条"}
    con.execute("UPDATE pending_resolutions SET status = ?, resolved_at = ? WHERE id = ?",
                (status, None if status == "open" else clock.now_utc_iso(), item_id))
    return {"ok": True, "id": item_id, "status": status}


# ---------------------------------------------------------------- 每周


def _statements(con: sqlite3.Connection, *, since: str | None,
                before: str | None, tags: set[str] | None, limit: int) -> list[dict]:
    """取用户说过的话 + 它所属段的标签。since/before 划定时间范围。"""
    sql = """
        SELECT m.id, m.created_at, m.content, group_concat(DISTINCT t.name) AS tags
          FROM messages m
          LEFT JOIN segments s ON s.conversation_id = m.conversation_id
                              AND m.seq BETWEEN s.start_seq AND s.end_seq
          LEFT JOIN segment_tags st ON st.segment_id = s.id
          LEFT JOIN tags t ON t.id = st.tag_id AND t.state = 'approved'
         WHERE m.state = 'active' AND m.role = 'user'
    """
    params: list[Any] = []
    if since:
        sql += " AND m.created_at >= ?"
        params.append(since)
    if before:
        sql += " AND m.created_at < ?"
        params.append(before)
    sql += " GROUP BY m.id ORDER BY m.created_at DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in con.execute(sql, params).fetchall():
        row_tags = set((r["tags"] or "").split(",")) - {""}
        if tags is not None and not (row_tags & tags):
            continue
        out.append({"id": int(r["id"]), "when": clock.local_str(r["created_at"]),
                    "text": (r["content"] or "").replace("\n", " ")[:120],
                    "tags": sorted(row_tags)})
    return out


def _render(rows: list[dict]) -> str:
    return "\n".join(f"- [{r['when']}] ({'/'.join(r['tags']) or '无标签'}) {r['text']}"
                     for r in rows) or "（无）"


def weekly(con: sqlite3.Connection, cfg, provider, *, days: int = 7,
           limit: int = 25) -> dict:
    """最近 N 天 vs 更早：找同一话题下的矛盾说法。只记录，不改记忆。"""
    start = clock.window_start_iso(days * 24)
    recent = _statements(con, since=start, before=None, tags=None, limit=limit)
    if not recent:
        return {"recent": 0, "older": 0, "new": 0, "items": []}
    tag_pool = set()
    for r in recent:
        tag_pool.update(r["tags"])
    older = _statements(con, since=None, before=start,
                        tags=tag_pool or None, limit=limit)

    msgs = [
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content":
            f"【最近 {days} 天的说法】\n{_render(recent)}\n\n"
            f"【更早的说法】\n{_render(older)}"},
    ]
    raw, _u = provider.tag_batch(msgs, max_tokens=1500)
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {"recent": len(recent), "older": len(older), "new": 0,
                "items": [], "error": "模型输出不是 JSON"}

    created = []
    for c in (data.get("contradictions") or [])[:20]:
        if not isinstance(c, dict):
            continue
        payload = {"source": "weekly", "days": days,
                   "topic": str(c.get("topic") or "")[:60],
                   "old": str(c.get("old") or "")[:200],
                   "new": str(c.get("new") or "")[:200],
                   "why": str(c.get("why") or "")[:160]}
        if not payload["topic"]:
            continue
        nid = add_pending(con, "contradiction", payload)
        if nid:
            created.append(nid)
    return {"recent": len(recent), "older": len(older),
            "new": len(created), "items": created}


# ---------------------------------------------------------------- 每月


def _tag_sets(con: sqlite3.Connection, conv_ids: list[int]) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    q = ",".join("?" * len(conv_ids)) or "NULL"
    for r in con.execute(
            f"""SELECT s.conversation_id AS cid, t.name AS name
                  FROM segments s
                  JOIN segment_tags st ON st.segment_id = s.id
                  JOIN tags t ON t.id = st.tag_id
                 WHERE s.conversation_id IN ({q})""", conv_ids):
        out.setdefault(int(r["cid"]), set()).add(r["name"])
    return out


def monthly(con: sqlite3.Connection, cfg, provider, *, limit: int = 50,
            workdir: Path | None = None) -> dict:
    """重新整理后对比：在**影子库**里重打这些会话，与现有结果比标签差异。

    影子库的意义：重整理会删段重建，直接在真库上做等于让一次实验覆盖历史结论。
    差异只记录，不改真库。
    """
    ids = [int(r[0]) for r in con.execute(
        "SELECT id FROM conversations WHERE state = 'active' "
        "ORDER BY started_at DESC LIMIT ?", (limit,))]
    if not ids:
        return {"conversations": 0, "new": 0, "items": []}
    before = _tag_sets(con, ids)

    tmp = (workdir or Path(con.execute("PRAGMA database_list").fetchone()[2]).parent) / "_review_shadow.db"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)
    # 影子库：整库备份，再把这些会话的派生标签清掉重打
    shadow = db.connect(tmp)
    try:
        con.backup(shadow)
        for cid in ids:
            tagging.forget(shadow, cid)
        ok = 0
        for cid in ids:
            r = tagging.rerun_one(shadow, provider, cid)
            if r.get("ok"):
                ok += 1
        after = _tag_sets(shadow, ids)
    finally:
        shadow.close()
        for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
            p.unlink(missing_ok=True)

    created, drifted = [], 0
    for cid in ids:
        old, new = before.get(cid, set()), after.get(cid, set())
        added, removed = sorted(new - old), sorted(old - new)
        if not added and not removed:
            continue
        drifted += 1
        title = con.execute("SELECT title FROM conversations WHERE id = ?",
                            (cid,)).fetchone()
        nid = add_pending(con, "drift", {
            "source": "monthly", "conversation_id": cid,
            "topic": ((title["title"] if title else "") or "")[:60],
            "old": "旧标签：" + ("、".join(sorted(old)) or "无"),
            "new": "新标签：" + ("、".join(sorted(new)) or "无"),
            "why": ("新增 " + "、".join(added) if added else "")
                   + ("；消失 " + "、".join(removed) if removed else ""),
        })
        if nid:
            created.append(nid)
    return {"conversations": len(ids), "retagged": ok,
            "drifted": drifted, "new": len(created), "items": created}
