"""实体层验收：人物/机构投影、归属区间、候选（不调 API）。

核心要验的不是"能不能建表"，而是**它只给候选、不下结论**：
同一个角色在同一家机构的两段区间里，`candidates_at` 必须返回多个人，
把"是不是同一个人"这个问题原样留给用户。

断言：
1. 标签投影成实体，且幂等（跑两次不翻倍）
2. 角色词被标成 is_role，具名的不被误标
3. 提及数正确（段→实体）
4. 归属区间按时间筛候选：半年前 → 老王，昨天 → 小李
5. 同一机构换过老板时，候选是 2 个（**不下结论**）
6. 没有归属区间的角色会被列进"需要人确认"
7. 全程不动原文（消息数、段数不变）

用法：python tools/smoke_entity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pmem import clock, db, entities, store, tagging  # noqa: E402
from pmem.config import load  # noqa: E402


def main() -> int:
    cfg = load()
    workdir = cfg.path("paths", "db").parent
    tmp = workdir / "_entity_smoke.db"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)
    con = db.open_db(tmp, force=True)

    # 造两场会话 + 段，挂上 人物/老板 和 人物/某教授、机构/前司
    cid = store.upsert_conversation(con, source="live", source_id="ent-smoke",
                                    title="实体验收", started_at=clock.now_utc_iso(),
                                    time_precision="message")
    for t in ("半年前吐槽老板", "昨天吐槽前司老板"):
        store.add_message(con, conversation_id=cid, role="user", origin="user",
                          content=t, created_at=clock.now_utc_iso(),
                          time_precision="message", source="live")
    for name, dim in (("老板", "人物"), ("某教授", "人物"), ("前司", "机构")):
        tagging.add_tag(con, name, dimension=dim, state="approved")
    con.execute("UPDATE tags SET state='approved' WHERE dimension IN ('人物','机构')")
    msgs = tagging._conversation_messages(con, cid)
    tagging.apply_segments(con, cid, msgs, {"segments": [
        {"from": 1, "to": 2, "title": "吐槽",
         "tags": [{"dim": "人物", "name": "老板"}, {"dim": "机构", "name": "前司"}]}]})
    # 具名的人：手工加一个段外实体，用来验 is_role 不被误标
    tagging.add_tag(con, "某教授", dimension="人物", state="approved")

    # 第二个会话：3 段都提到"猎头"（角色词），但没有任何归属区间
    cid2 = store.upsert_conversation(con, source="live", source_id="ent-smoke-2",
                                     title="猎头接触", started_at=clock.now_utc_iso(),
                                     time_precision="message")
    for i, t in enumerate(("猎头A来电话", "猎头B来电话", "猎头C来电话")):
        store.add_message(con, conversation_id=cid2, role="user", origin="user",
                          content=t, created_at=clock.now_utc_iso(),
                          time_precision="message", source="live")
    tagging.add_tag(con, "猎头", dimension="人物", state="approved")
    msgs2 = tagging._conversation_messages(con, cid2)
    tagging.apply_segments(con, cid2, msgs2, {"segments": [
        {"from": 1, "to": 1, "title": "a", "tags": [{"dim": "人物", "name": "猎头"}]},
        {"from": 2, "to": 2, "title": "b", "tags": [{"dim": "人物", "name": "猎头"}]},
        {"from": 3, "to": 3, "title": "c", "tags": [{"dim": "人物", "name": "猎头"}]}]})

    results: list[tuple[str, bool, str]] = []
    n_msg_before = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    n_seg_before = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]

    r1 = entities.sync_from_tags(con)
    r2 = entities.sync_from_tags(con)
    results.append(("标签投影成实体且幂等", r2["entities_new"] == 0
                    and r2["entities_total"] == r1["entities_total"]
                    and r2["mentions_new"] == 0,
                    f"首次 +{r1['entities_new']}，重复 +{r2['entities_new']}，共 {r2['entities_total']}"))

    boss = con.execute("SELECT id, is_role FROM entities WHERE name='老板' AND kind='person'").fetchone()
    prof = con.execute("SELECT id, is_role FROM entities WHERE name='某教授' AND kind='person'").fetchone()
    results.append(("角色实体 vs 具名实体",
                    boss is not None and boss["is_role"] == 1
                    and prof is not None and prof["is_role"] == 0,
                    f"老板 is_role={boss['is_role'] if boss else '?'}，"
                    f"某教授 is_role={prof['is_role'] if prof else '?'}"))

    hits = con.execute("SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ?",
                       (boss["id"],)).fetchone()[0]
    results.append(("提及数正确", hits == 1, f"老板被提到 {hits} 段"))

    # 归属区间：前司的老王（到今天仍是）、前司的小李（只覆盖过去）
    entities.add_tenure(con, "老板", "前司", valid_from="2025-06-01",
                        valid_to=None, origin="derived")
    entities.add_tenure(con, "老板", "前司", valid_from="2025-11-01",
                        valid_to="2026-01-01", origin="derived")
    half_year = "2025-07-01T00:00:00Z"
    yesterday = (clock.now_utc_iso())
    near = entities.candidates_at(con, half_year, org="前司")
    far = entities.candidates_at(con, yesterday, org="前司")
    results.append(("区间覆盖：半年前 → 老王在", any(c["valid_from"] == "2025-06-01" for c in near),
                    f"候选 {len(near)} 条"))
    results.append(("区间覆盖：昨天 → 小李已不在", 
                    all(c["valid_from"] != "2025-11-01" for c in far),
                    f"候选 {len(far)} 条"))

    # 同一机构、同一角色、区间重叠 → 必须给出 2 个候选（不下结论）
    both = entities.candidates_at(con, "2025-12-01T00:00:00Z", org="前司")
    results.append(("换过老板时给多个候选", len(both) >= 2, f"候选 {len(both)} 条"))

    un = entities.unresolved_roles(con, limit=20)
    results.append(("无归属的频繁角色进待确认",
                    any(u["name"] == "猎头" and u["orgs"] == 0 for u in un),
                    f"共 {len(un)} 个角色需确认：{[u['name'] for u in un]}"))

    n_msg_after = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    n_seg_after = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    results.append(("不动原文", n_msg_before == n_msg_after and n_seg_before == n_seg_after,
                    f"消息 {n_msg_before}→{n_msg_after}，段 {n_seg_before}→{n_seg_after}"))

    con.close()
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    width = max(len(r[0]) for r in results)
    ok_all = True
    print("实体层：人物/机构 + 归属区间\n")
    for name, ok, detail in results:
        if not ok:
            ok_all = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
