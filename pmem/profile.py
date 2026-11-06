"""用户状态：断言 + 有效期 + 指向原文的证据。

它不是"档案摘要"。三条硬约束（用户 2026-11-06）：

1. **证据指向原文**：`evidence` 是消息 id 列表，任何一条都能点回原话。
   查不到证据的断言只能标 `origin=derived`（推断），不能冒充事实。
2. **推断不得覆盖已确认**：`origin=derived` 想写一个与现有 `confirmed` 不同的值时，
   **直接拒绝**并返回那条确认值——冲突摆到人面前，不悄悄改。
3. **旧值不删**：确认新值时，旧值只被 `superseded_by` 串起来并补上 `valid_to`，
   这样才回答得了"我以前怎么想、什么时候变的"。

文件（profile/*.md）只是这张表的**导出视图**，不反向写库——否则就有第二个事实来源。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import clock

KINDS = ("就业", "居住", "身份", "偏好", "关系", "其他")


def _active_confirmed(con: sqlite3.Connection, kind: str, subject: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM profile_facts WHERE kind = ? AND subject = ? "
        "AND origin = 'confirmed' AND superseded_by IS NULL "
        "ORDER BY id DESC LIMIT 1", (kind, subject)).fetchone()


def _check_evidence(con: sqlite3.Connection, ids: list[int]) -> list[int]:
    """只保留真实存在的消息 id——证据必须能在原文里找到。"""
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    found = {int(r[0]) for r in con.execute(
        f"SELECT id FROM messages WHERE id IN ({q})", ids)}
    return sorted(i for i in ids if i in found)


def add_fact(con: sqlite3.Connection, kind: str, subject: str, value: str, *,
             valid_from: str | None = None, valid_to: str | None = None,
             evidence: list[int] | None = None,
             origin: str = "derived") -> dict[str, Any]:
    kind = (kind or "").strip()
    subject = (subject or "").strip()
    value = (value or "").strip()
    if kind not in KINDS:
        return {"ok": False, "reason": f"kind 只能是 {KINDS}"}
    if not subject or not value:
        return {"ok": False, "reason": "subject 和 value 不能为空"}
    if origin not in ("derived", "confirmed"):
        return {"ok": False, "reason": "origin 只能是 derived / confirmed"}

    ev = _check_evidence(con, [int(i) for i in (evidence or [])])
    if origin == "derived" and not ev:
        return {"ok": False,
                "reason": "推断必须给出证据（原文消息 id）；没有证据就不要写成状态"}

    cur = _active_confirmed(con, kind, subject)
    if origin == "derived" and cur is not None and cur["value"] != value:
        return {"ok": False,
                "reason": "已有用户确认的值，推断不得覆盖",
                "confirmed": {"id": int(cur["id"]), "value": cur["value"]}}

    superseded: list[int] = []
    if origin == "confirmed":
        for r in con.execute(
                "SELECT id FROM profile_facts WHERE kind = ? AND subject = ? "
                "AND superseded_by IS NULL AND value <> ?",
                (kind, subject, value)).fetchall():
            con.execute("UPDATE profile_facts SET superseded_by = -1, valid_to = COALESCE(valid_to, ?) "
                        "WHERE id = ?", (clock.now_utc_iso(), int(r["id"])))
            superseded.append(int(r["id"]))

    cur2 = con.execute(
        "INSERT INTO profile_facts(kind, subject, value, valid_from, valid_to, "
        "evidence, origin, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (kind, subject, value, valid_from, valid_to,
         json.dumps(ev, ensure_ascii=False), origin, clock.now_utc_iso()))
    fid = int(cur2.lastrowid)
    if superseded:
        con.execute("UPDATE profile_facts SET superseded_by = ? WHERE superseded_by = -1 "
                    "AND kind = ? AND subject = ?", (fid, kind, subject))
    return {"ok": True, "id": fid, "superseded": superseded, "evidence": ev}


def confirm(con: sqlite3.Connection, fact_id: int) -> dict[str, Any]:
    row = con.execute("SELECT * FROM profile_facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "没有这条"}
    if row["origin"] == "confirmed":
        return {"ok": True, "id": fact_id, "already": True}
    con.execute("UPDATE profile_facts SET origin = 'confirmed' WHERE id = ?", (fact_id,))
    return {"ok": True, "id": fact_id, "already": False}


def list_facts(con: sqlite3.Connection, *, subject: str | None = None,
               kind: str | None = None, include_superseded: bool = False,
               limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM profile_facts WHERE 1=1"
    params: list[Any] = []
    if not include_superseded:
        sql += " AND superseded_by IS NULL"
    if subject:
        sql += " AND subject = ?"
        params.append(subject)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in con.execute(sql, params).fetchall():
        d = {k: r[k] for k in r.keys()}
        try:
            d["evidence"] = json.loads(d["evidence"] or "[]")
        except Exception:
            d["evidence"] = []
        out.append(d)
    return out


def history(con: sqlite3.Connection, kind: str, subject: str) -> list[dict[str, Any]]:
    """一个断言的全部版本（含被取代的），按时间正序——回答"什么时候变的"。"""
    out = []
    for r in con.execute(
            "SELECT * FROM profile_facts WHERE kind = ? AND subject = ? ORDER BY id",
            (kind, subject)).fetchall():
        d = {k: r[k] for k in r.keys()}
        try:
            d["evidence"] = json.loads(d["evidence"] or "[]")
        except Exception:
            d["evidence"] = []
        out.append(d)
    return out


# ---------------------------------------------------------------- 导出视图

def _render_group(kind: str, rows: list[dict[str, Any]]) -> str:
    lines = [f"# {kind}", "",
             "> 由 `pmem profile export` 生成，**不要手改**（表才是真相，改了下次会被覆盖）。",
             "> 「来源」列：`confirmed` = 你确认过的；`derived` = 机器推断，可能错。",
             ""]
    lines.append("| 主题 | 断言 | 有效期 | 来源 | 证据 |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        span = f"{r['valid_from'] or '?'} → {r['valid_to'] or '今'}"
        ev = "、".join(f"#{i}" for i in (r.get("evidence") or [])) or "—"
        origin = "确认" if r["origin"] == "confirmed" else "推断"
        lines.append(f"| {r['subject']} | {r['value']} | {span} | {origin} | {ev} |")
    lines.append("")
    return "\n".join(lines)


def export_md(con: sqlite3.Connection, cfg, *, out_dir=None) -> dict[str, Any]:
    """把 `profile_facts` 导成一组 Markdown（人读视图）。

    这是**单向**的：只读表、只写文件。文件不反向写库——
    否则就出现第二个事实来源，和"原文唯一"打架。

    只导出当前有效的（`superseded_by IS NULL`）：被取代的旧值留在表里，
    要看历史用 `pmem profile history`，不往文件里堆。
    """
    from pathlib import Path

    root = Path(out_dir) if out_dir else cfg.path("paths", "profile")
    root.mkdir(parents=True, exist_ok=True)
    rows = list_facts(con, limit=1000)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["kind"], []).append(r)

    written: list[str] = []
    for kind in KINDS:
        if kind not in grouped:
            continue
        p = root / f"{kind}.md"
        p.write_text(_render_group(kind, grouped[kind]), encoding="utf-8")
        written.append(p.name)

    if not written:
        index = ["# 用户状态", "", "（还没有任何状态。用 `pmem profile add` 记一条，再导出。）"]
        (root / "README.md").write_text("\n".join(index), encoding="utf-8")
        written.append("README.md")

    return {"ok": True, "dir": str(root), "files": written, "facts": len(rows)}
