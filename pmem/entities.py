"""实体层：把「人物/机构」从标签里抬成独立实体，并记录归属区间。

为什么要有这一层（用户 2026-11-06 的例子）：

    半年前「吐槽老板」，昨天「吐槽前司老板」——是不是同一个人？

标签回答不了这个问题：标签只知道"这一段提到了老板"，不知道"哪个老板、在哪个机构、
什么时间段"。实体层补上三样东西：

- `entities`        这个人/机构是谁（`is_role=1` 表示"老板""猎头"这类角色实体）
- `entity_tenure`   某人在某机构的任职区间（**有效期未知就留 NULL，不假装知道**）
- `entity_mentions` 哪一段提到过它

有了区间，机器能推到"两边都指向同一家机构的老板"；**推不到"是不是同一个自然人"**
（机构换过老板），所以最终仍要问人——这正是把问题摆进 `pending` 的依据。

本模块只写派生层：标签、段、原文都不动。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import Any

from . import clock

# 角色词：这些「人物」标签其实是角色，不是具名的人。B 类歧义主要来自它们。
ROLE_WORDS = {
    "老板", "上司", "领导", "技术总监", "经理", "主任工程师", "创办人", "创始人",
    "同事", "同学", "同门M", "师弟", "师弟L", "朋友", "友人s", "友人S", "客户",
    "客户博士", "销售", "作者", "前任", "弟弟", "表弟", "长辈", "小妹妹",
    "研究生", "新手", "猎头", "男性", "用户", "中介", "供应商",
}


# ---------------------------------------------------------------- 实体


def ensure_entity(con: sqlite3.Connection, kind: str, name: str,
                  aliases: list[str] | None = None) -> int:
    """建或取一个实体。名字唯一，所以反复同步不会产生重复行。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("实体名不能为空")
    if kind not in ("person", "org"):
        raise ValueError("kind 只能是 person / org")
    is_role = 1 if (kind == "person" and name in ROLE_WORDS) else 0
    # 大小写不同的同名实体要合并（"原公司" 和 "原公司" 是同一家公司）。
    # 用 NOCASE 查、保留先出现的写法，避免同一实体被拆成两份。
    same = con.execute("SELECT id FROM entities WHERE kind = ? AND name = ? COLLATE NOCASE",
                       (kind, name)).fetchone()
    if same:
        return int(same["id"])
    con.execute(
        "INSERT INTO entities(kind, name, is_role, aliases, created_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(kind, name) DO UPDATE SET "
        "is_role = excluded.is_role, "
        "aliases = COALESCE(excluded.aliases, entities.aliases)",
        (kind, name, is_role,
         json.dumps(aliases or [], ensure_ascii=False) if aliases else None,
         clock.now_utc_iso()))
    row = con.execute("SELECT id FROM entities WHERE kind = ? AND name = ? COLLATE NOCASE",
                      (kind, name)).fetchone()
    return int(row["id"])


def sync_from_tags(con: sqlite3.Connection) -> dict:
    """把已批准的「人物/机构」标签投影成实体，并重建"段→实体"提及。

    幂等：实体按 (kind,name) 去重，提及按 (segment,entity) 去重。
    机构标签里"某激光公司""某材料公司"这种既是机构又被当人物的，各自成一条，不互相覆盖。
    """
    created = 0
    for r in con.execute(
            "SELECT name, dimension FROM tags WHERE state = 'approved' "
            "AND dimension IN ('人物','机构')").fetchall():
        kind = "person" if r["dimension"] == "人物" else "org"
        before = con.execute("SELECT 1 FROM entities WHERE kind = ? AND name = ?",
                             (kind, r["name"])).fetchone()
        ensure_entity(con, kind, r["name"])
        if not before:
            created += 1

    mentions = 0
    for r in con.execute(
            """SELECT st.segment_id AS sid, t.dimension AS dim, t.name AS name
                 FROM segment_tags st
                 JOIN tags t ON t.id = st.tag_id
                WHERE t.state = 'approved' AND t.dimension IN ('人物','机构')""").fetchall():
        kind = "person" if r["dim"] == "人物" else "org"
        eid = con.execute("SELECT id FROM entities WHERE kind = ? AND name = ?",
                          (kind, r["name"])).fetchone()["id"]
        cur = con.execute(
            "INSERT OR IGNORE INTO entity_mentions(segment_id, entity_id, created_at) "
            "VALUES (?,?,?)", (int(r["sid"]), int(eid), clock.now_utc_iso()))
        mentions += cur.rowcount
    return {"entities_new": created,
            "entities_total": con.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "mentions_new": mentions}


# ---------------------------------------------------------------- 归属区间


def add_tenure(con: sqlite3.Connection, person: str, org: str | None, *,
               valid_from: str | None = None, valid_to: str | None = None,
               evidence: list[int] | None = None,
               origin: str = "derived",
               basis: str | None = None) -> dict:
    """登记"某人在某机构任职"的区间。日期未知就留空——宁可没有，也不假装有。"""
    pid = ensure_entity(con, "person", person)
    oid = ensure_entity(con, "org", org) if org else None
    con.execute(
        "INSERT INTO entity_tenure(entity_id, org_id, valid_from, valid_to, "
        "evidence, origin, basis, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (pid, oid, valid_from, valid_to,
         json.dumps(evidence or [], ensure_ascii=False), origin,
         (basis or None), clock.now_utc_iso()))
    return {"person_id": pid, "org_id": oid,
            "valid_from": valid_from, "valid_to": valid_to}


def confirm_tenure(con: sqlite3.Connection, tenure_id: int) -> dict:
    """把一条推断的区间标成"用户确认"。确认后推断不得覆盖它。"""
    row = con.execute("SELECT id, origin FROM entity_tenure WHERE id = ?",
                      (tenure_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "没有这条区间"}
    con.execute("UPDATE entity_tenure SET origin = 'confirmed' WHERE id = ?",
                (tenure_id,))
    return {"ok": True, "id": tenure_id, "origin": "confirmed"}


def _parse(iso: str | None) -> _dt.datetime | None:
    """统一成带时区的 UTC，否则 naive/aware 混在一起比不了大小（踩过）。"""
    if not iso:
        return None
    try:
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def _covers(tenure: sqlite3.Row, when: str | None) -> bool:
    """区间是否覆盖某时刻。两端为空视为"无限延伸"（不知道就别排除）。"""
    if not when:
        return True
    t = _parse(when)
    lo, hi = _parse(tenure["valid_from"]), _parse(tenure["valid_to"])
    if t is None:
        return True
    if lo and t < lo:
        return False
    if hi and t > hi:
        return False
    return True


def candidates_at(con: sqlite3.Connection, when: str | None = None, *,
                  org: str | None = None, only_roles: bool = False) -> list[dict]:
    """某时刻（可选：某机构内）所有可能的人。

    这是给 B 类问题用的：「半年前说的老板，候选是这几个，是哪个？」
    注意它**只给候选，不下结论**——机构换过老板时这里会返回多个人。
    """
    sql = """SELECT p.id AS pid, p.name AS person, p.is_role, o.name AS org,
                    t.valid_from, t.valid_to, t.origin
               FROM entity_tenure t
               JOIN entities p ON p.id = t.entity_id
               LEFT JOIN entities o ON o.id = t.org_id
              WHERE p.kind = 'person'"""
    params: list[Any] = []
    if org:
        sql += " AND o.name = ?"
        params.append(org)
    if only_roles:
        sql += " AND p.is_role = 1"
    out = []
    for r in con.execute(sql, params).fetchall():
        if _covers(r, when):
            out.append({"person_id": int(r["pid"]), "person": r["person"],
                        "org": r["org"], "valid_from": r["valid_from"],
                        "valid_to": r["valid_to"], "origin": r["origin"],
                        "is_role": bool(r["is_role"])})
    return out


def unresolved_roles(con: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    """该进队列的候选：角色实体出现了，但没有归属区间，或跨了多个机构。

    这些正是"机器判不了、只能问人"的情况。**只列出来，不写任何结论。**
    """
    out = []
    for r in con.execute(
            """SELECT e.id, e.name,
                      (SELECT COUNT(*) FROM entity_mentions m WHERE m.entity_id = e.id) AS hits,
                      (SELECT COUNT(DISTINCT org_id) FROM entity_tenure t
                        WHERE t.entity_id = e.id) AS orgs
                 FROM entities e
                WHERE e.kind = 'person' AND e.is_role = 1
                ORDER BY hits DESC LIMIT ?""", (limit,)).fetchall():
        hits, orgs = int(r["hits"]), int(r["orgs"])
        if orgs == 0 and hits >= 3:
            out.append({"entity_id": int(r["id"]), "name": r["name"], "hits": hits,
                        "orgs": orgs,
                        "why": "出现过但不知道属于哪家机构/哪个时间段"})
        elif orgs >= 2:
            out.append({"entity_id": int(r["id"]), "name": r["name"], "hits": hits,
                        "orgs": orgs,
                        "why": f"在 {orgs} 家机构都有任职区间，可能不是同一个人"})
    return out
