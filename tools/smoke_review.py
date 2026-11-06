"""复查（每周矛盾 / 每月漂移）与待裁决队列的验收。

不调 API：用一个假 provider 直接产出"模型会说的话"，
这样测的是**记录、去重、回答、关闭**这些真正容易出错的地方。

断言：
1. 每周复查能把模型指出的矛盾记进队列
2. 同一条矛盾重复跑不会记两次（去重）
3. 用户回答只记录，不自动关闭
4. 关闭后从 open 列表消失
5. 每月复查在**影子库**里重打，差异记为「漂移」
6. 影子库不污染真库（真库段数不变）
7. 两段式：propose 只写建议、不动数据
8. 两段式：apply 才真合并（提及不丢、名字进 alias、写审计）
9. 两段式：执行过的不能再执行（状态机）
10. 两段式：set_tenure 写成「用户确认」
11. 两段式：驳回之后不能再执行
12. 两段式：闭集外的动作在提议阶段就被拒

用法：python tools/smoke_review.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pmem import actions, clock, db, entities, review, store, tagging  # noqa: E402
from pmem.config import load  # noqa: E402


class ContradictionProvider:
    """只会在 weekly 里用：直接给一条矛盾。"""
    name = "stub"

    def tag_batch(self, messages, *, max_tokens: int = 900):
        return (json.dumps({"contradictions": [
            {"topic": "手冲咖啡偏好",
             "old": "我对不加糖手冲咖啡的世界没什么滤镜",
             "new": "想买一瓶单品试试",
             "why": "从否定到主动购买，态度反转"}]}, ensure_ascii=False),
            {"model": "stub", "tokens_in": 0, "tokens_out": 0})

    def classify(self, *a, **k):
        return ({}, {})


class RetagProvider:
    """只会在 monthly 里用：把每场都重打成一个与旧标签不同的主题。"""
    name = "stub"

    def tag_batch(self, messages, *, max_tokens: int = 900):
        n = max(1, min(len(messages), 1))
        return (json.dumps({"segments": [
            {"from": 1, "to": 1, "title": "重打结果",
             "tags": [{"dim": "主题", "name": "工作"}]}]}, ensure_ascii=False),
            {"model": "stub", "tokens_in": 0, "tokens_out": 0})

    def classify(self, *a, **k):
        return ({}, {})


def main() -> int:
    cfg = load()
    workdir = cfg.path("paths", "db").parent
    tmp = workdir / "_review_smoke.db"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)
    con = db.open_db(tmp, force=True)

    # 造两场会话：一场更早（手冲咖啡负面），一场最近（想买）
    for sid, title, when, text in (
            ("rv-old", "手冲咖啡初聊", "2026-09-01T12:00:00.000Z",
             "我对所谓的不加糖手冲咖啡的世界没什么滤镜"),
            ("rv-new", "想买一瓶", clock.now_utc_iso(),
             "考虑买一瓶单品手冲咖啡试试")):
        cid = store.upsert_conversation(con, source="live", source_id=sid, title=title,
                                       started_at=when, time_precision="message")
        store.add_message(con, conversation_id=cid, role="user", origin="user",
                          content=text, created_at=when, time_precision="message",
                          source="live")

    results: list[tuple[str, bool, str]] = []

    r1 = review.weekly(con, cfg, ContradictionProvider(), days=7, limit=20)
    results.append(("每周复查能记录矛盾", r1["new"] == 1, f"新增 {r1['new']} 条"))

    r2 = review.weekly(con, cfg, ContradictionProvider(), days=7, limit=20)
    items = review.list_pending(con, status="open", limit=50)
    results.append(("同一条不会记两次", r2["new"] == 0 and len(items) == 1,
                    f"第二次新增 {r2['new']}，队列 {len(items)} 条"))

    if items:
        item_id = items[0]["id"]
        review.answer(con, item_id, "这是同一个人，之前只是说不追求不加糖")
        after_answer = review.list_pending(con, status="open", limit=50)
        rec = [x for x in after_answer if x["id"] == item_id]
        ok = bool(rec) and rec[0]["answer"] and rec[0]["status"] == "open"
        results.append(("回答只记录、不自动关闭", ok,
                        f"状态={rec[0]['status'] if rec else '?'}"))

        review.set_status(con, item_id, "resolved")
        still_open = review.list_pending(con, status="open", limit=50)
        results.append(("关闭后从 open 列表消失",
                        all(x["id"] != item_id for x in still_open),
                        f"open 剩 {len(still_open)} 条"))
    else:
        results.append(("回答只记录、不自动关闭", False, "没有可答的条目"))
        results.append(("关闭后从 open 列表消失", False, "没有可关的条目"))

    # monthly：影子库重打，与现有标签对比。先给旧会话打一个"投资"标签
    cid_old = con.execute("SELECT id FROM conversations WHERE source_id='rv-old'").fetchone()[0]
    tagging.add_tag(con, "投资", dimension="主题", state="approved")
    msgs = tagging._conversation_messages(con, cid_old)
    tagging.apply_segments(con, cid_old, msgs, {"segments": [
        {"from": 1, "to": 1, "title": "旧标签", "tags": [{"dim": "主题", "name": "投资"}]}]})
    seg_before = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]

    rm = review.monthly(con, cfg, RetagProvider(), limit=10, workdir=workdir)
    seg_after = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    results.append(("每月复查记录漂移", rm["drifted"] >= 1 and rm["new"] >= 1,
                    f"对比 {rm['conversations']} 场，漂移 {rm['drifted']} 场"))
    results.append(("影子库不污染真库", seg_before == seg_after,
                    f"段数 {seg_before}→{seg_after}"))

    # ---- 7–12. 两段式：说明 → 提议动作 → 你确认才执行
    class _ActionProv:
        """假模型：直接把给定的动作当成它的建议。"""
        name = "stub"

        def __init__(self, action: dict):
            self.action = action

        def tag_batch(self, messages, *, max_tokens=900):
            return (json.dumps(self.action, ensure_ascii=False),
                    {"model": "stub", "tokens_in": 0, "tokens_out": 0})

    # 两段会话 + 一个段，好让实体有"提及"可搬
    def _seg(source_id: str, text: str) -> tuple[int, int]:
        c = store.upsert_conversation(con, source="live", source_id=source_id,
                                      title="动作验收", started_at=clock.now_utc_iso(),
                                      time_precision="message")
        store.add_message(con, conversation_id=c, role="user", origin="user",
                          content=text, created_at=clock.now_utc_iso(),
                          time_precision="message", source="live")
        sid = int(con.execute(
            "INSERT INTO segments(conversation_id, start_seq, end_seq, title, created_at) "
            "VALUES (?,?,?,?,?)", (c, 1, 1, "动作验收段", clock.now_utc_iso())).lastrowid)
        return c, sid

    _, seg_a = _seg("act-a", "某激光公司那边怎么说")
    _, seg_b = _seg("act-b", "某激光的人也提过")

    e_main = entities.ensure_entity(con, "org", "某激光公司")
    e_short = entities.ensure_entity(con, "org", "某激光")
    for sid, eid in ((seg_a, e_main), (seg_b, e_short)):
        con.execute("INSERT OR IGNORE INTO entity_mentions(segment_id, entity_id, created_at) "
                    "VALUES (?,?,?)", (sid, eid, clock.now_utc_iso()))

    item_a = review.add_pending(con, "entity", {
        "sig_key": "entity:act-a", "topic": "「某激光」是不是同一家",
        "old": "", "new": "", "why": "两个名字指同一家公司吗"})
    review.answer(con, item_a, "是同一家，后者是简称")

    before_n = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    prop = actions.propose(con, cfg, _ActionProv(
        {"kind": "merge_entity", "params": {"target": e_main, "sources": [e_short]},
         "why": "简称"}), item_a)
    stored = con.execute("SELECT proposal, status FROM pending_resolutions WHERE id = ?",
                         (item_a,)).fetchone()
    after_n = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    results.append(("propose 只写建议、不动数据",
                    prop.get("ok") and after_n == before_n and bool(stored["proposal"])
                    and stored["status"] == "proposed",
                    f"实体数 {before_n}→{after_n}，状态={stored['status']}"))

    ap = actions.apply(con, item_a, actor="smoke")
    alias = con.execute("SELECT aliases FROM entities WHERE id = ?", (e_main,)).fetchone()[0]
    merged_mentions = con.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ?", (e_main,)).fetchone()[0]
    e_short_gone = con.execute("SELECT 1 FROM entities WHERE id = ?", (e_short,)).fetchone() is None
    audit_n = con.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
    results.append(("apply 才真合并（提及不丢、名字进 alias、写审计）",
                    ap.get("ok") and e_short_gone and merged_mentions == 2
                    and "某激光" in (alias or "") and audit_n >= 1,
                    f"提及 {merged_mentions} 条，alias={alias}，审计 {audit_n} 条"))

    again = actions.apply(con, item_a, actor="smoke")
    results.append(("执行过的不能再执行", again.get("ok") is False,
                    str(again.get("reason", ""))[:26]))

    item_b = review.add_pending(con, "entity", {
        "sig_key": "entity:act-b", "topic": "「这家」是哪段",
        "old": "", "new": "", "why": "归属区间"})
    review.answer(con, item_b, "从现在开始就在这家")
    actions.propose(con, cfg, _ActionProv(
        {"kind": "set_tenure",
         "params": {"entity": e_main, "org": e_main, "from": "2024-01", "to": None,
                    "evidence": [], "basis": "某激光"}, "why": "补区间"}), item_b)
    ap2 = actions.apply(con, item_b, actor="smoke")
    ten = con.execute("SELECT origin, valid_from FROM entity_tenure "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    results.append(("set_tenure 写成「用户确认」",
                    ap2.get("ok") and ten is not None and ten["origin"] == "confirmed",
                    f"origin={ten['origin'] if ten else '?'} from={ten['valid_from'] if ten else '?'}"))

    item_c = review.add_pending(con, "entity", {
        "sig_key": "entity:act-c", "topic": "「某激光公司」是不是老板",
        "old": "", "new": "", "why": "角色还是具名"})
    review.answer(con, item_c, "先不动")
    actions.propose(con, cfg, _ActionProv(
        {"kind": "noop_close", "params": {}, "why": "只记录"}), item_c)
    rj = actions.reject(con, item_c, "先不改")
    after_reject = actions.apply(con, item_c, actor="smoke")
    results.append(("驳回之后不能再执行", rj.get("ok") and after_reject.get("ok") is False,
                    f"reject={rj.get('status')}，apply={after_reject.get('reason', '')[:18]}"))

    item_d = review.add_pending(con, "entity", {
        "sig_key": "entity:act-d", "topic": "随便", "old": "", "new": "", "why": "测闭集"})
    review.answer(con, item_d, "随便")
    bad = actions.propose(con, cfg, _ActionProv(
        {"kind": "delete_everything", "params": {}}), item_d)
    results.append(("闭集外的动作在提议阶段就被拒",
                    bad.get("ok") is False and "未知动作" in str(bad.get("reason", "")),
                    str(bad.get("reason", ""))[:30]))

    con.close()
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    width = max(len(r[0]) for r in results)
    ok_all = True
    print("复查与待裁决队列\n")
    for name, ok, detail in results:
        if not ok:
            ok_all = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
