"""两段式：把「你的说明」变成「一条可确认的动作」。

为什么要两段：说明和动作不是一回事。你说"这是同一个人"，系统该做的是
**提出一个具体动作**（合并哪两个实体、区间怎么改），再等你点头；
而不是听完就自己猜着改。所以：

    open ──answer──> （answered，隐含在 answer 列里）
                     └──propose──> proposed ──apply──> applied
                                        └──reject──> rejected

三条硬约束：

1. **propose 绝不落数据**：只写 `pending_resolutions.proposal` 一列。
2. **apply 才改**，而且必须写 `action_log`（谁、何时、什么动作、结果如何）。
3. **失败整条回滚**：要么全做、要么什么都不做，不留半拉子修改。

动作种类是**闭集**（`KINDS`）。模型只能在里面选，选不出就 `noop_close`。
这也是"闭集"这个思路第二次出现——第一次是标签词表。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import clock, entities

KINDS = {
    "merge_entity": "把几个实体合成一个：提及与区间归到 target，其余名字记成 alias",
    "split_entity": "把一个实体拆开：把指定提及移到一个新实体",
    "set_tenure": "登记或修正一条归属区间（标成用户确认）",
    "merge_tag": "把待审标签并进已有标签；没有可并的就直接批准",
    "merge_conversation": "把一场会话并入另一场（drop 的消息接到 keep 尾部）",
    "noop_close": "只关闭这条，不改任何数据",
}

PROPOSAL_SYSTEM = """你在把一个「待裁决的记忆问题」变成**一条可执行的动作**。

只输出 JSON，不要解释。形状：
{"kind": "<动作名>", "params": {...}, "why": "<一句话理由>"}

可选动作（kind 只能是这些）：
- merge_entity      params: {"target": 实体id, "sources": [实体id...]}
- split_entity      params: {"entity": 实体id, "mentions": [提及id...], "new_name": "新名字"}
- set_tenure        params: {"entity": 实体id, "org": 机构id或null, "from": "YYYY-MM"或null,
                            "to": "YYYY-MM"或null, "evidence": [消息id...], "basis": "原话片段"}
- merge_tag         params: {"name": "现有标签名", "into": "目标标签名或null", "dim": "主题|地点|人物|机构|任务|情绪"}
- merge_conversation params: {"keep": 会话id, "drop": 会话id}
- noop_close        params: {}

规则：
- **信息不够就不要编**：拿不准就用 noop_close，并在 why 里写清缺什么。
- 只用下面给出的 id；不要自己发明 id。
- 日期只写能确定的，不确定就给 null。
"""


# ---------------------------------------------------------------- 取数据


def _entity(con: sqlite3.Connection, eid: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()


def _item(con: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT id, kind, payload, status, answer, proposal FROM pending_resolutions "
        "WHERE id = ?", (item_id,)).fetchone()


def _inventory(con: sqlite3.Connection, limit: int = 40) -> str:
    """给模型看的"可引用对象"清单。没有它，模型会自己编 id。"""
    ents = con.execute(
        "SELECT id, kind, name, is_role FROM entities ORDER BY id LIMIT ?",
        (limit,)).fetchall()
    lines = ["【实体】id / kind / 名字 / 是否角色"]
    for e in ents:
        lines.append(f"  {e['id']} / {e['kind']} / {e['name']} / {e['is_role']}")
    tags = con.execute(
        "SELECT name, dimension, state FROM tags WHERE state IN ('approved','pending') "
        "ORDER BY dimension, name LIMIT ?", (limit,)).fetchall()
    lines.append("【标签】名字 / 维度 / 状态")
    for t in tags:
        lines.append(f"  {t['name']} / {t['dimension']} / {t['state']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 提议


def _ask_model(con: sqlite3.Connection, provider, row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    body = "\n".join([
        f"【问题】#{row['id']}（{row['kind']}）",
        f"话题：{payload.get('topic') or '（未标注）'}",
        f"较早的说法：{payload.get('old') or '—'}",
        f"较近的说法：{payload.get('new') or '—'}",
        f"为什么要人裁决：{payload.get('why') or '—'}",
        f"【我的回答】{row['answer'] or '（还没回答）'}",
        "",
        _inventory(con),
    ])
    msgs = [{"role": "system", "content": PROPOSAL_SYSTEM},
            {"role": "user", "content": body}]
    raw, _usage = provider.tag_batch(msgs, max_tokens=600)
    text = (raw or "").strip()
    if not text or text == "{}":
        return {"ok": False, "reason": "模型没有给出建议（离线或返回空）"}
    try:
        data = json.loads(text)
    except Exception:
        return {"ok": False, "reason": f"模型输出不是 JSON：{text[:80]}"}
    if not isinstance(data, dict):
        return {"ok": False, "reason": "模型输出不是对象"}
    return {"ok": True, "action": data}


def validate(con: sqlite3.Connection, action: dict) -> tuple[bool, str]:
    """结构 + 存在性校验。**执行前还要再跑一次**——提议之后数据可能变了。"""
    if not isinstance(action, dict):
        return False, "动作不是对象"
    kind = action.get("kind")
    if kind not in KINDS:
        return False, f"未知动作 {kind!r}（只能是 {', '.join(KINDS)}）"
    p = action.get("params") or {}
    if not isinstance(p, dict):
        return False, "params 不是对象"

    if kind == "noop_close":
        return True, ""

    if kind == "merge_entity":
        tgt = p.get("target")
        srcs = p.get("sources") or []
        if not isinstance(tgt, int) or not _entity(con, tgt):
            return False, f"target 实体不存在：{tgt!r}"
        if not srcs or not all(isinstance(s, int) and _entity(con, s) for s in srcs):
            return False, f"sources 里有不存在的实体：{srcs!r}"
        if tgt in srcs:
            return False, "target 不能出现在 sources 里"
        return True, ""

    if kind == "split_entity":
        eid = p.get("entity")
        if not isinstance(eid, int) or not _entity(con, eid):
            return False, f"entity 不存在：{eid!r}"
        mids = p.get("mentions") or []
        if not mids or not all(isinstance(m, int) for m in mids):
            return False, "mentions 必须是非空 id 列表"
        missing = [m for m in mids if not con.execute(
            "SELECT 1 FROM entity_mentions WHERE id = ? AND entity_id = ?",
            (m, eid)).fetchone()]
        if missing:
            return False, f"这些提及不属于该实体：{missing}"
        if not (p.get("new_name") or "").strip():
            return False, "new_name 不能为空"
        return True, ""

    if kind == "set_tenure":
        eid = p.get("entity")
        if not isinstance(eid, int) or not _entity(con, eid):
            return False, f"entity 不存在：{eid!r}"
        org = p.get("org")
        if org is not None and (not isinstance(org, int) or not _entity(con, org)):
            return False, f"org 不存在：{org!r}"
        if p.get("from") and p.get("to") and str(p["from"]) > str(p["to"]):
            return False, "from 不能晚于 to"
        return True, ""

    if kind == "merge_tag":
        name = (p.get("name") or "").strip()
        if not name:
            return False, "name 不能为空"
        row = con.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if not row:
            return False, f"标签不存在：{name}"
        into = p.get("into")
        if into:
            tgt = con.execute("SELECT id FROM tags WHERE name = ?", (into,)).fetchone()
            if not tgt:
                return False, f"目标标签不存在：{into}"
            if into == name:
                return False, "不能并到自己"
        return True, ""

    if kind == "merge_conversation":
        keep, drop = p.get("keep"), p.get("drop")
        for k, v in (("keep", keep), ("drop", drop)):
            if not isinstance(v, int) or not con.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (v,)).fetchone():
                return False, f"{k} 会话不存在：{v!r}"
        if keep == drop:
            return False, "不能并到自己"
        n_seg = con.execute("SELECT COUNT(*) FROM segments WHERE conversation_id = ?",
                            (drop,)).fetchone()[0]
        if n_seg:
            return False, f"drop 那边有 {n_seg} 个话题段，先整理再合并"
        return True, ""

    return False, "未处理的动作"


def describe(action: dict) -> str:
    """给人看的一句话预览。执行前你必须能读懂它要干什么。"""
    kind = action.get("kind")
    p = action.get("params") or {}
    if kind == "merge_entity":
        return f"合并实体：{p.get('sources')} → {p.get('target')}"
    if kind == "split_entity":
        return f"拆出实体：从 #{p.get('entity')} 移走 {p.get('mentions')} → 新实体「{p.get('new_name')}」"
    if kind == "set_tenure":
        return (f"登记区间：实体 #{p.get('entity')} 在机构 {p.get('org')}，"
                f"{p.get('from') or '?'} → {p.get('to') or '今'}（确认为准）")
    if kind == "merge_tag":
        return (f"标签「{p.get('name')}」并入「{p.get('into')}」" if p.get("into")
                else f"批准标签「{p.get('name')}」")
    if kind == "merge_conversation":
        return f"会话 #{p.get('drop')} 并入 #{p.get('keep')}"
    if kind == "noop_close":
        return "不改任何数据，只关闭这条"
    return f"（未知动作 {kind}）"


def propose(con: sqlite3.Connection, cfg, provider, item_id: int) -> dict[str, Any]:
    """生成一条动作建议。**只写 proposal 列，绝不改记忆。**"""
    row = _item(con, item_id)
    if not row:
        return {"ok": False, "reason": "没有这一条"}
    if row["status"] != "open":
        return {"ok": False, "reason": f"状态是 {row['status']}，只有 open 的条目能提议"}
    if not (row["answer"] or "").strip():
        return {"ok": False, "reason": "先用 pending answer 写下你的说明，再让系统提议动作"}

    got = _ask_model(con, provider, row)
    if not got.get("ok"):
        return got
    action = got["action"]
    ok, why = validate(con, action)
    if not ok:
        return {"ok": False, "reason": f"模型给的动作不可执行：{why}", "action": action}

    con.execute(
        "UPDATE pending_resolutions SET proposal = ?, proposed_at = ?, status = 'proposed' "
        "WHERE id = ?",
        (json.dumps(action, ensure_ascii=False), clock.now_utc_iso(), item_id))
    return {"ok": True, "id": item_id, "status": "proposed",
            "action": action, "preview": describe(action),
            "why": action.get("why", "")}


def reject(con: sqlite3.Connection, item_id: int, reason: str | None = None) -> dict[str, Any]:
    row = _item(con, item_id)
    if not row:
        return {"ok": False, "reason": "没有这一条"}
    if row["status"] != "proposed":
        return {"ok": False, "reason": f"状态是 {row['status']}，只有 proposed 能驳回"}
    con.execute(
        "UPDATE pending_resolutions SET status = 'rejected', applied_at = ?, apply_result = ? "
        "WHERE id = ?",
        (clock.now_utc_iso(), json.dumps({"rejected": reason or ""}, ensure_ascii=False), item_id))
    return {"ok": True, "id": item_id, "status": "rejected"}


# ---------------------------------------------------------------- 执行


def _exec_merge_entity(con: sqlite3.Connection, p: dict) -> dict:
    target = int(p["target"])
    t = _entity(con, target)
    aliases = set(json.loads(t["aliases"] or "[]"))
    mentions = tenure = 0
    for sid in [int(s) for s in p["sources"]]:
        s = _entity(con, sid)
        aliases.add(s["name"])
        mentions += con.execute(
            "INSERT OR IGNORE INTO entity_mentions(segment_id, entity_id, created_at) "
            "SELECT segment_id, ?, created_at FROM entity_mentions WHERE entity_id = ?",
            (target, sid)).rowcount
        tenure += con.execute(
            "UPDATE entity_tenure SET entity_id = ? WHERE entity_id = ?", (target, sid)).rowcount
        tenure += con.execute(
            "UPDATE entity_tenure SET org_id = ? WHERE org_id = ?", (target, sid)).rowcount
        con.execute("DELETE FROM entity_mentions WHERE entity_id = ?", (sid,))
        con.execute("DELETE FROM entities WHERE id = ?", (sid,))
    con.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                (json.dumps(sorted(aliases), ensure_ascii=False), target))
    return {"target": target, "aliases": sorted(aliases),
            "mentions_moved": mentions, "tenure_moved": tenure}


def _exec_split_entity(con: sqlite3.Connection, p: dict) -> dict:
    e = _entity(con, int(p["entity"]))
    new_id = entities.ensure_entity(con, e["kind"], str(p["new_name"]).strip())
    moved = 0
    for mid in [int(m) for m in p["mentions"]]:
        row = con.execute(
            "SELECT segment_id FROM entity_mentions WHERE id = ? AND entity_id = ?",
            (mid, e["id"])).fetchone()
        if not row:
            continue
        moved += con.execute(
            "INSERT OR IGNORE INTO entity_mentions(segment_id, entity_id, created_at) "
            "VALUES (?,?,?)", (int(row["segment_id"]), new_id, clock.now_utc_iso())).rowcount
        con.execute("DELETE FROM entity_mentions WHERE id = ?", (mid,))
    return {"from": int(e["id"]), "new_entity": new_id, "mentions_moved": moved}


def _exec_set_tenure(con: sqlite3.Connection, p: dict) -> dict:
    person = _entity(con, int(p["entity"]))
    org_id = p.get("org")
    org = _entity(con, int(org_id)) if org_id else None
    evidence = json.dumps([int(i) for i in (p.get("evidence") or [])], ensure_ascii=False)
    res = entities.add_tenure(
        con, person["name"], org["name"] if org else None,
        valid_from=p.get("from") or None, valid_to=p.get("to") or None,
        evidence=json.loads(evidence), origin="confirmed", basis=p.get("basis"))
    return {"tenure": res, "origin": "confirmed"}


def _exec_merge_tag(con: sqlite3.Connection, p: dict) -> dict:
    name = str(p["name"]).strip()
    src = con.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    into = (p.get("into") or "").strip()
    if not into:
        con.execute("UPDATE tags SET state = 'approved' WHERE id = ?", (int(src["id"]),))
        return {"approved": name}
    tgt = con.execute("SELECT id FROM tags WHERE name = ?", (into,)).fetchone()
    moved = con.execute(
        "INSERT OR IGNORE INTO segment_tags(segment_id, tag_id, weight, source, created_at) "
        "SELECT segment_id, ?, weight, source, created_at FROM segment_tags WHERE tag_id = ?",
        (int(tgt["id"]), int(src["id"]))).rowcount
    con.execute("DELETE FROM segment_tags WHERE tag_id = ?", (int(src["id"]),))
    con.execute("DELETE FROM tags WHERE id = ?", (int(src["id"]),))
    return {"merged": name, "into": into, "links_moved": moved}


def _exec_merge_conversation(con: sqlite3.Connection, p: dict) -> dict:
    keep, drop = int(p["keep"]), int(p["drop"])
    base = con.execute("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE conversation_id = ?",
                       (keep,)).fetchone()[0]
    rows = con.execute("SELECT id FROM messages WHERE conversation_id = ? ORDER BY seq",
                       (drop,)).fetchall()
    for i, r in enumerate(rows, 1):
        con.execute("UPDATE messages SET conversation_id = ?, seq = ? WHERE id = ?",
                    (keep, int(base) + i, int(r["id"])))
    con.execute("DELETE FROM conversations WHERE id = ?", (drop,))
    return {"keep": keep, "drop": drop, "messages_moved": len(rows)}


_EXEC = {
    "merge_entity": _exec_merge_entity,
    "split_entity": _exec_split_entity,
    "set_tenure": _exec_set_tenure,
    "merge_tag": _exec_merge_tag,
    "merge_conversation": _exec_merge_conversation,
    "noop_close": lambda con, p: {"noop": True},
}


def apply(con: sqlite3.Connection, item_id: int, *, actor: str = "cli") -> dict[str, Any]:
    """执行已经提议的动作。**要么全做，要么什么都不做**，并写 action_log。"""
    row = _item(con, item_id)
    if not row:
        return {"ok": False, "reason": "没有这一条"}
    if row["status"] != "proposed":
        return {"ok": False, "reason": f"状态是 {row['status']}，只有 proposed 能执行"}
    if not row["proposal"]:
        return {"ok": False, "reason": "没有提议内容"}
    try:
        action = json.loads(row["proposal"])
    except Exception:
        return {"ok": False, "reason": "提议内容不是合法 JSON"}

    # 提议之后数据可能变了（别处删了实体/会话），执行前再校验一次
    ok, why = validate(con, action)
    if not ok:
        return {"ok": False, "reason": f"现在执行会失败：{why}"}

    con.execute("BEGIN")
    try:
        result = _EXEC[action["kind"]](con, action.get("params") or {})
        con.execute(
            "UPDATE pending_resolutions SET status = 'applied', applied_at = ?, "
            "apply_result = ?, actor = ? WHERE id = ?",
            (clock.now_utc_iso(), json.dumps(result, ensure_ascii=False), actor, item_id))
        con.execute(
            "INSERT INTO action_log(item_id, kind, payload, result, actor, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (item_id, action["kind"],
             json.dumps(action, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), actor, clock.now_utc_iso()))
        con.execute("COMMIT")
    except Exception as e:  # noqa: BLE001
        con.execute("ROLLBACK")
        return {"ok": False, "reason": f"执行失败，已回滚：{type(e).__name__}: {e}"}

    return {"ok": True, "id": item_id, "status": "applied",
            "kind": action["kind"], "result": result}


def log(con: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """执行审计：谁在什么时候把哪个动作执行了。"""
    out = []
    for r in con.execute(
            "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        d = {k: r[k] for k in r.keys()}
        for k in ("payload", "result"):
            try:
                d[k] = json.loads(d[k] or "{}")
            except Exception:
                pass
        out.append(d)
    return out
