"""P1 验收：任务对象。

验证五件事：
1. 任务能登记、能收尾（状态/摘要/时间都落库）
2. 任务有自己的工作日志会话（source='task'）
3. **热窗排除任务过程**（关键：不然下午闲聊会被上午的工作污染）
4. **热窗保留状态变更消息**（"已下发""已完成"两条要在连续对话里）
5. 冷检索仍能命中任务内容（问"报告里说了什么"要查得到）

用临时库跑，跑完删掉，不碰真实数据。

用法：python tools/smoke_task.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pmem import clock, db, search, store, tasks  # noqa: E402
from pmem.config import load  # noqa: E402

PROCESS_TEXT = "内部试错记录：先读了三年财报，再去掉一次性损益重新算了一遍"
SUMMARY = "营收三年 CAGR 约 12%，毛利稳定；yy 方向研发投入占比从 4% 升到 9%"


def main() -> int:
    cfg = load()
    tmp = cfg.path("paths", "db").parent / "_task_smoke.db"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    con = db.open_db(tmp, force=True)
    results: list[tuple[str, bool, str]] = []

    # 上午的闲聊（主对话）
    live = store.upsert_conversation(
        con, source="live", source_id="smoke-live", title="验收对话",
        started_at=clock.now_utc_iso(), time_precision="message")
    store.add_message(con, conversation_id=live, role="user", origin="user",
                      content="早上好", created_at=clock.now_utc_iso(),
                      time_precision="message", source="live")

    # 下发任务（parent 指向主对话）
    t = tasks.start(con, goal="检查某公司近几年业务变化，出个简单报告",
                    executor="inline", parent_conversation_id=live)
    tid = t["id"]

    # 任务过程写进它自己的工作日志
    tasks.log(con, tid, PROCESS_TEXT)

    # 收尾
    t = tasks.finish(con, tid, status="done", summary=SUMMARY)

    # 1 状态落库
    results.append(("任务状态落库", t["status"] == "done" and t["result_summary"] == SUMMARY
                    and bool(t["finished_at"]), f"status={t['status']}"))

    # 2 工作日志会话
    wl = con.execute("SELECT source, task_id FROM conversations WHERE id = ?",
                     (t["conversation_id"],)).fetchone()
    results.append(("任务有独立工作日志会话",
                    wl is not None and wl["source"] == "task" and wl["task_id"] == tid,
                    f"source={wl['source'] if wl else None}"))

    # 3 / 4 热窗
    hot = search.hot_window(con, cfg, hours=24)
    hot_text = "\n".join(m["content"] for m in hot)
    results.append(("热窗排除任务过程", PROCESS_TEXT not in hot_text,
                    f"热窗 {len(hot)} 条"))
    results.append(("热窗保留下发与完成消息",
                    f"已下发任务 #{tid}" in hot_text and f"任务 #{tid} 已完成" in hot_text,
                    "两条指针都在" if f"任务 #{tid} 已完成" in hot_text else "缺指针"))

    # 5 冷检索能命中任务内容
    hits = search.search(con, cfg, "试错记录", limit=5)
    results.append(("冷检索能命中任务内容", any(PROCESS_TEXT in h["content"] for h in hits),
                    f"命中 {len(hits)} 条"))

    # 6 / 7 对外出口（用离线 provider，不花 token）
    from pmem import llm, ports
    prov = llm.OfflineProvider()
    before = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    ctx = ports.build_context(con, cfg, prov, "我上次对000000怎么说的")
    after = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    results.append(("context 只读不写库",
                    before == after and "messages" in ctx and len(ctx["messages"]) == 3,
                    f"消息数 {before} -> {after}"))

    r = ports.note(con, cfg, text="验收用的一条消息")
    row = con.execute("SELECT created_at, role FROM messages WHERE id = ?",
                      (r["message_id"],)).fetchone()
    results.append(("note 自己盖时间戳",
                    bool(row and row["created_at"]) and row["role"] == "user",
                    f"created_at={row['created_at'] if row else None}"))

    # 人设：给了就必须出现在首条 system 里；撤掉就不该还留着。
    # （人设只能改语气，不能改记忆规则——这一条靠 prompt 里的顺序保证，测试只验"接上了"）
    cfg.data["chat"]["persona_file"] = ""
    cfg.data["chat"]["persona"] = "测试人设：说话极短。"
    ctx_on = ports.build_context(con, cfg, prov, "在吗")
    cfg.data["chat"]["persona"] = ""
    ctx_off = ports.build_context(con, cfg, prov, "在吗")
    results.append((
        "人设能进系统提示词（且可关）",
        "测试人设：说话极短。" in ctx_on["messages"][0]["content"]
        and "测试人设" not in ctx_off["messages"][0]["content"],
        f"开={len(ctx_on['messages'][0]['content'])}字 关={len(ctx_off['messages'][0]['content'])}字"))

    con.close()
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    width = max(len(r[0]) for r in results)
    ok_all = True
    print("P1 验收：任务对象\n")
    for name, ok, detail in results:
        if not ok:
            ok_all = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
