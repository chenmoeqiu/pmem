"""从原文里推断任职经历（`derived`），给人一条起点而不是让人从零手填。

三条硬约束：

1. **只写 `origin='derived'`**：推断永远不覆盖 `confirmed`（人的确认值），
   冲突时两条并存，由 `pmem entity candidates` / `pending` 摆给人看。
2. **每条都带证据**：`evidence` 是消息 id 列表，能点回原话。没有证据就不写。
3. **不确定就留空**：日期没提到的留 NULL，不推算到具体某天。
   宁可少一条区间，也不要发明一个日期——那会污染后面所有时间判断。

为什么只做"任职经历"：这是唯一有明确文本线索、且能直接用上 `entity_tenure` 的一类。
偏好/居住等状态先不自动推，它们更容易推错，也要更多人工确认。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import clock, entities

HINT_WORDS = ("离职", "入职", "跳槽", "offer", "前司", "前公司", "现司", "裸辞",
              "gap", "上家", "从原公司", "原公司", "实习", "转正")

INFER_SYSTEM = """你在从用户的对话里提取**任职经历**。每条消息带 id 和本地日期。

规则：
- 只提取用户**明确说到**的任职信息（哪段时间在哪个单位、什么岗位）
- `org` 用用户自己的叫法（如"前司""现司""原公司"），**不要编公司全名**
- 日期：只写能确定的。写到"月"就写 YYYY-MM；只知道哪天离职就只填 to；
  **不确定就留空字符串**，不要推算到具体某天
- `evidence` 必须是**你引用的那几条消息 id**
- `basis` 是支撑这条的原话片段（≤40 字）
- 没有可提取的就返回 {"episodes":[]}

只输出 JSON：
{"episodes":[{"org":"前司","role":"工艺工程师","from":"","to":"2025-03-05",
"evidence":[1234,1235],"basis":"我在3月5日离职"}]}"""

ORG_ALIASES = ("前司", "现司", "上家", "前公司", "现公司")


def candidates(con: sqlite3.Connection, *, limit: int = 45) -> list[dict[str, Any]]:
    """含任职线索的用户消息，按时间正序。"""
    rows = con.execute(
        """SELECT id, created_at, content FROM messages
            WHERE state = 'active' AND role = 'user' AND created_at IS NOT NULL
            ORDER BY created_at""").fetchall()
    out = []
    for r in rows:
        text = r["content"] or ""
        if any(w in text for w in HINT_WORDS):
            out.append({"id": int(r["id"]), "when": clock.local_str(r["created_at"]),
                        "text": text.replace("\n", " ")[:200]})
    return out[-limit:] if limit else out


def _batch_text(msgs: list[dict]) -> str:
    return "\n".join(f"- [id={m['id']}] ({m['when']}) {m['text']}" for m in msgs)


def run(con: sqlite3.Connection, cfg, provider, *, limit: int = 45,
        batch: int = 15, dry: bool = False) -> dict[str, Any]:
    msgs = candidates(con, limit=limit)
    stat = {"scanned": len(msgs), "batches": 0, "episodes": 0,
            "written": 0, "skipped_dup": 0, "skipped_confirmed": 0,
            "no_evidence": 0, "errors": [], "items": []}
    if not msgs:
        return stat

    for i in range(0, len(msgs), batch):
        chunk = msgs[i:i + batch]
        stat["batches"] += 1
        try:
            raw, _u = provider.tag_batch(
                [{"role": "system", "content": INFER_SYSTEM},
                 {"role": "user", "content": "【消息】\n" + _batch_text(chunk)}],
                max_tokens=1200)
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as e:  # noqa: BLE001
            stat["errors"].append(f"批次 {stat['batches']} 失败：{type(e).__name__}")
            continue

        for ep in (data.get("episodes") or [])[:10]:
            if not isinstance(ep, dict):
                continue
            org = str(ep.get("org") or "").strip()[:20]
            ev = [int(x) for x in (ep.get("evidence") or [])
                  if str(x).isdigit()][:6]
            if not org:
                continue
            stat["episodes"] += 1
            if not ev:
                stat["no_evidence"] += 1
                continue
            where = ("WHERE org_id = (SELECT id FROM entities WHERE kind='org' AND name=?) "
                     "AND COALESCE(valid_from,'')=? AND COALESCE(valid_to,'')=?")
            args = (org, str(ep.get("from") or ""), str(ep.get("to") or ""))
            # 先看有没有**用户确认过**的同区间：有就绝不写（推断不得覆盖确认）
            if con.execute(f"SELECT id FROM entity_tenure {where} AND origin='confirmed'",
                           args).fetchone():
                stat["skipped_confirmed"] += 1
                continue
            if con.execute(f"SELECT id FROM entity_tenure {where}", args).fetchone():
                stat["skipped_dup"] += 1
                continue
            if dry:
                stat["items"].append({"org": org, "from": ep.get("from"),
                                      "to": ep.get("to"), "evidence": ev})
                stat["written"] += 1
                continue
            entities.add_tenure(
                con, "我", org, valid_from=(ep.get("from") or None),
                valid_to=(ep.get("to") or None), evidence=ev, origin="derived",
                basis=str(ep.get("basis") or "")[:80])
            stat["written"] += 1
            stat["items"].append({"org": org, "from": ep.get("from"),
                                  "to": ep.get("to"), "evidence": ev,
                                  "basis": str(ep.get("basis") or "")[:40]})
    return stat
