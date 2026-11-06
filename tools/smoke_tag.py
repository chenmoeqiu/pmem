"""P2 验收：多维标签 + 待审 + 话题段。

不打 API，只验证不变量：
1. 起步词表带维度
2. 一场会话能切出多个段（原文不复制，只记区间）
3. 词表内的值 → 直接生效、可检索
4. 词表外的值 → 进 pending、**检索查不到**
5. 批准之后立刻可检索
6. 段能正确映射回消息区间（不多不少）
7. 打标过的会话不重复入队
8. 默认不调用的会话：没点到触发词不出现，点到才放行
9. 清醒段边界 = 跨天 **AND** 间隔（跨天40分 / 同日8h 都不算，跨天9.5h 才算）
10. 滑出热区的内容仍可检索（不重不漏的一半）
11. 热区内的内容不重复返回（不重不漏的另一半）
12. 词汇鸿沟：原文没有这个词时全文查不到
13. 词汇鸿沟：同一件事靠标签能查到

用法：python tools/smoke_tag.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pmem import clock, db, search, store, tagging  # noqa: E402
from pmem.config import load  # noqa: E402


def main() -> int:
    cfg = load()
    tmp = cfg.path("paths", "db").parent / "_tag_smoke.db"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    con = db.open_db(tmp, force=True)
    results: list[tuple[str, bool, str]] = []

    n = tagging.seed(con)
    vocab = tagging.vocabulary(con)
    results.append(("起步词表带维度", n > 0 and all(t["dimension"] for t in vocab),
                    f"{n} 个，维度 {sorted({t['dimension'] for t in vocab})}"))

    conv = store.upsert_conversation(con, source="live", source_id="tag-smoke",
                                     title="打标验收", started_at=clock.now_utc_iso(),
                                     time_precision="message")
    texts = [
        "十月我想去甲省玩一圈，乙市丙市丁市",
        "住宿我住快捷酒店就行，别太贵",
        "000000 现在还能买吗",
        "分红的时候我在忙，忘了卖",
        "那算了，先观察",
    ]
    ids = []
    for t in texts:
        ids.append(store.add_message(con, conversation_id=conv, role="user", origin="user",
                                     content=t, created_at=clock.now_utc_iso(),
                                     time_precision="message", source="live"))
    msgs = tagging._conversation_messages(con, conv)

    # 模型输出：前两条 = 旅行段，后三条 = 投资段
    data = {"segments": [
        {"from": 1, "to": 2, "title": "甲省行程", "reason": "从行程切到股票",
         "tags": [{"dim": "地点", "name": "甲省", "parent": "中国"},
                  {"dim": "主题", "name": "旅行"},
                  {"dim": "任务", "name": "行程规划"}]},
        {"from": 3, "to": 5, "title": "000000 操作", "reason": "",
         "tags": [{"dim": "主题", "name": "投资"},
                  {"dim": "机构", "name": "某航运公司"}]},
    ]}
    st = tagging.apply_segments(con, conv, msgs, data)
    results.append(("一段会话切出多段", st["segments"] == 2, f"segments={st['segments']}"))
    results.append(("词表外的新值进待审", st["new_pending"] == 3,
                    f"new_pending={st['new_pending']}"))

    travel = search.search_by_tag(con, cfg, "旅行", limit=10)
    in_range = {h["content"] for h in travel} == set(texts[:2])
    results.append(("段能精确映射回消息", in_range, f"命中 {len(travel)} 条"))

    pend = search.search_by_tag(con, cfg, "甲省", limit=10)
    row = con.execute("SELECT id, state, dimension FROM tags WHERE name='甲省'").fetchone()
    results.append(("待审值不参与检索",
                    len(pend) == 0 and row is not None and row["state"] == "pending",
                    f"命中 {len(pend)} 条 / state={row['state'] if row else None}"))

    tagging.set_state(con, int(row["id"]), "approved")
    after = search.search_by_tag(con, cfg, "甲省", limit=10)
    results.append(("批准后立刻可检索", len(after) == 2, f"命中 {len(after)} 条"))

    dup = tagging.untagged_conversations(con, limit=10)
    results.append(("打标过的会话不重复入队", len(dup) == 0, f"剩 {len(dup)} 场"))

    # v7：默认不调用 —— 用户当前这句话没点到触发词，就不该被检索出来
    con.execute("UPDATE conversations SET no_recall = 1, recall_triggers = '差分机' "
                "WHERE id = ?", (conv,))
    silent = search.search_multi(con, cfg, ["甲省"], limit=10, mention="今天有点累")
    loud = search.search_multi(con, cfg, ["甲省"], limit=10,
                               mention="差分机里那个设定还算得通吗")
    results.append(("不调用的会话默认不出现", len(silent) == 0, f"命中 {len(silent)} 条"))
    results.append(("点到触发词才放行", len(loud) > 0, f"命中 {len(loud)} 条"))
    con.execute("UPDATE conversations SET no_recall = 0, recall_triggers = NULL WHERE id = ?",
                (conv,))

    # ---- 9. 睡眠边界：间隔 ≥6h **且** 与睡眠窗口重叠 ≥2h（熬夜场景必须分段）
    win = cfg.get("chat", "sleep_window", default="01:00-08:00")
    a = search._is_sleep_gap("2026-11-06T16:20:00Z", "2026-11-06T15:40:00Z", 6, 8, win, 2)  # 跨天40分
    b = search._is_sleep_gap("2026-11-07T01:10:00Z", "2026-11-06T15:40:00Z", 6, 8, win, 2)  # 正常睡眠
    c = search._is_sleep_gap("2026-11-06T10:00:00Z", "2026-11-06T02:00:00Z", 6, 8, win, 2)  # 同日下午8h
    d = search._is_sleep_gap("2026-11-06T04:33:00Z", "2026-11-05T16:59:00Z", 6, 8, win, 2)  # 熬夜后起床
    results.append(("睡眠边界=间隔+睡眠窗口", (a, b, c, d) == (False, True, False, True),
                    f"跨天40分={a} 正常睡={b} 同日下午={c} 熬夜后中午={d}"))

    # ---- 10/11. 不重不漏：滑出热区的可检索，热区内的不重复返回
    since = clock.window_start_iso(24)
    bconv = store.upsert_conversation(con, source="live", source_id="boundary-smoke",
                                      title="边界验收", started_at=clock.now_utc_iso(),
                                      time_precision="message")
    store.add_message(con, conversation_id=bconv, role="user", origin="user",
                      content="泽塔协议的第一版设想", created_at="2026-10-01T00:00:00.000Z",
                      time_precision="message", source="live")
    store.add_message(con, conversation_id=bconv, role="user", origin="user",
                      content="泽塔协议又改了一版", created_at=clock.now_utc_iso(),
                      time_precision="message", source="live")
    bhits = search.search_multi(con, cfg, ["泽塔协议"], limit=10, exclude_since=since)
    btext = [h["content"] for h in bhits]
    results.append(("滑出热区的内容仍可检索", any("第一版" in t for t in btext),
                    f"命中 {len(bhits)} 条"))
    results.append(("热区内的内容不重复返回", all("又改了一版" not in t for t in btext),
                    f"命中 {len(bhits)} 条"))

    # ---- 权重钩子：留给记忆曲线/情绪权重。默认恒 1（不改变现状），给了就必须乘进去。
    plain = search.search(con, cfg, "泽塔协议", limit=5)
    zeroed = search.search(con, cfg, "泽塔协议", limit=5, weight=lambda r: 0.0)
    doubled = search.search(con, cfg, "泽塔协议", limit=5, weight=lambda r: 2.0)
    same_order = [h["id"] for h in plain] == [h["id"] for h in doubled]
    results.append(("权重钩子接在打分上（默认不变，给了才乘）",
                    bool(plain) and all(h["score"] == 0 for h in zeroed)
                    and same_order
                    and all(abs(d["score"] - 2 * p["score"]) < 1e-9
                            for p, d in zip(plain, doubled)),
                    f"{len(plain)} 条；×0 后全 0，×2 后顺序不变"))

    # ---- 12/13. 词汇鸿沟：原文里没有这个词，标签能把它捞回来
    gconv = store.upsert_conversation(con, source="live", source_id="vocab-gap-smoke",
                                      title="换个说法", started_at=clock.now_utc_iso(),
                                      time_precision="message")
    store.add_message(con, conversation_id=gconv, role="user", origin="user",
                      content="那家做激光设备的老大最近怎么样",
                      created_at="2026-10-02T00:00:00.000Z",
                      time_precision="message", source="live")
    info = tagging.add_tag(con, "某激光公司", dimension="机构", state="approved")
    tagging.set_state(con, int(info["id"]), "approved")
    gmsgs = tagging._conversation_messages(con, gconv)
    tagging.apply_segments(con, gconv, gmsgs, {"segments": [
        {"from": 1, "to": 1, "title": "那家激光公司",
         "tags": [{"dim": "机构", "name": "某激光公司"}]}]})
    gfts = search.search_multi(con, cfg, ["某激光公司"], limit=10)
    gtag = search.search_by_tag(con, cfg, "某激光公司", limit=10)
    results.append(("词汇鸿沟：全文查不到", len(gfts) == 0, f"全文 {len(gfts)} 条"))
    results.append(("词汇鸿沟：标签查得到", len(gtag) >= 1, f"标签 {len(gtag)} 条"))

    # ---- 14/15/16. 转冷入队：睡了一觉之后，边界之前**没打标**的会话才入队
    cconv = store.upsert_conversation(con, source="live", source_id="cold-smoke",
                                      title="待整理的一夜", started_at=clock.now_utc_iso(),
                                      time_precision="message")
    store.add_message(con, conversation_id=cconv, role="user", origin="user",
                      content="睡前说的那段话", created_at="2026-10-03T00:00:00.000Z",
                      time_precision="message", source="live")
    boundary = clock.window_start_iso(24)
    queued1 = tagging.mark_cold_before(con, boundary)
    pending = tagging.pending_cold(con, limit=50)
    results.append(("睡眠边界后未打标的旧会话入队", cconv in pending and queued1 >= 1,
                    f"入队 {queued1} 场，队列 {pending}"))
    results.append(("已打标的会话不重复入队", conv not in pending,
                    f"已打标的 conv={conv} 不在队列"))
    from pmem import llm
    ran = tagging.run_pending(con, cfg, llm.build(cfg, force_offline=True), limit=50)
    results.append(("队列跑完后清空", not tagging.pending_cold(con, limit=50),
                    f"跑了 {ran['conversations']} 场"))

    # ---- 17/18. 闲时整理：转冷之后**自己**消化，且间隔内不重复跑
    # 这一条守的是"转冷的语义"：睡过去的那一段该被整理，不该等人敲命令。
    import json as _json
    from pmem import daily

    class _TagProv:
        name = "stub"

        def tag_batch(self, messages, *, max_tokens=900):
            return (_json.dumps({"segments": [
                {"from": 1, "to": len(messages), "title": "整理", "reason": "",
                 "tags": [{"dim": "主题", "name": "工作"}]}]}, ensure_ascii=False),
                {"model": "stub", "tokens_in": 0, "tokens_out": 0})

        def classify(self, *a, **k):
            return ({}, {})

    def _enqueue_old(name: str, text: str) -> None:
        c = store.upsert_conversation(con, source="live", source_id=name,
                                      title="待整理", started_at=clock.now_utc_iso(),
                                      time_precision="message")
        store.add_message(con, conversation_id=c, role="user", origin="user",
                          content=text, created_at="2026-10-05T00:00:00.000Z",
                          time_precision="message", source="live")

    _enqueue_old("daily-smoke-1", "昨天晚上说的那段")
    tagging.mark_cold_before(con, clock.window_start_iso(24))
    before = daily.pending_count(con)
    auto = daily.maybe_auto(con, cfg, _TagProv())
    results.append(("转冷后自动整理（不需要敲命令）",
                    int(auto.get("processed") or 0) >= 1
                    and daily.pending_count(con) < before,
                    f"处理 {auto.get('processed')} 场，队列 {before}→{daily.pending_count(con)}"))

    _enqueue_old("daily-smoke-2", "又一次睡过去的那段")
    tagging.mark_cold_before(con, clock.window_start_iso(24))
    at_once = daily.pending_count(con)
    again = daily.maybe_auto(con, cfg, _TagProv())
    results.append(("间隔内不重复整理（不会连着烧调用）",
                    int(again.get("processed") or 0) == 0
                    and daily.pending_count(con) == at_once,
                    f"跳过：{str(again.get('skipped', ''))[:26]}；队列 {at_once}→{daily.pending_count(con)}"))

    # 离线时必须原地不动：拿离线 provider 跑只会得到空标签，
    # 却会把会话标成 done —— 那等于永久丢掉这一段的打标机会。
    from pmem import llm as _llm
    con.execute("DELETE FROM meta WHERE key = ?", (daily.META_KEY,))
    offline = daily.maybe_auto(con, cfg, _llm.build(cfg, force_offline=True))
    results.append(("离线时不动队列（不会标成已整理却没标签）",
                    int(offline.get("processed") or 0) == 0
                    and daily.pending_count(con) == at_once,
                    f"跳过：{str(offline.get('skipped', ''))[:24]}；队列 {at_once}→{daily.pending_count(con)}"))

    # ---- 17. 证据不足时先反问细节，而不是只丢一句"不够"（用户 2026-11-06 的偏好）
    class _StubProvider:
        name = "stub"
        def classify(self, *a, **k):
            return ({"domain": "casual", "need_recall": True,
                     "keywords": ["泽塔协议"], "tags": [], "reason": "stub"},
                    {"model": "stub", "tokens_in": 0, "tokens_out": 0})
        def reconsider(self, *a, **k):
            return ({"needs_history": False, "keywords": [], "reason": ""},
                    {"model": "stub", "tokens_in": 0, "tokens_out": 0})
        def assess(self, q, hits):
            return ({"sufficient": False, "used": [], "missing": "具体是哪一件事",
                     "next": "ask_user"},
                    {"model": "stub", "tokens_in": 0, "tokens_out": 0})
        def synthesize(self, m):
            return "不该走到生成", {"model": "stub", "tokens_in": 0, "tokens_out": 0}

    from pmem import chat as _chat
    sess = _chat.ChatSession(con, cfg, _StubProvider())
    sess.conversation_id = bconv          # 用含"泽塔协议"的会话，保证有命中
    tr = sess.respond("泽塔协议那件事怎么样了")
    asked = ("？" in tr.reply) and ("补一个细节" in tr.reply)
    results.append(("证据不足时先反问细节", asked and tr.kind == "clarify",
                    f"kind={tr.kind} reply={tr.reply[:34]}…"))

    con.close()
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    width = max(len(r[0]) for r in results)
    ok_all = True
    print("P2 验收：多维标签 + 话题段\n")
    for name, ok, detail in results:
        if not ok:
            ok_all = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
