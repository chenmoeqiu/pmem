"""用户状态（profile_facts）与"实体问题进队列"的验收。不调 API。

要守的三条红线，每条都有对应用例：
1. 推断必须带证据，且证据必须真的指向原文（伪造的消息 id 会被丢弃）
2. 推断不得覆盖已确认
3. 旧值不删：确认新值时旧值只被串起来，history 看得到全过程

另外验：
4. list 默认只显示当前有效（被取代的不出现）
5. `entity ask` 把角色实体挂进 pending，且幂等（重复跑不重复报）

用法：python tools/smoke_profile.py
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

from pmem import clock, db, entities, profile, review, store, tagging  # noqa: E402
from pmem.config import load  # noqa: E402


def main() -> int:
    cfg = load()
    workdir = cfg.path("paths", "db").parent
    tmp = workdir / "_profile_smoke.db"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)
    con = db.open_db(tmp, force=True)

    # 造一场会话 + 三条消息（作为证据来源）
    cid = store.upsert_conversation(con, source="live", source_id="pf-smoke",
                                    title="状态验收", started_at=clock.now_utc_iso(),
                                    time_precision="message")
    ids = []
    for t in ("我 3 月 5 日离职", "现在在戊市做激光加工", "老板今天又画饼"):
        ids.append(store.add_message(con, conversation_id=cid, role="user", origin="user",
                                     content=t, created_at=clock.now_utc_iso(),
                                     time_precision="message", source="live"))

    results: list[tuple[str, bool, str]] = []

    r = profile.add_fact(con, "就业", "我", "离职于 2025-03-05")
    results.append(("推断不给证据就拒绝", r.get("ok") is False,
                    r.get("reason", "")[:30]))

    r = profile.add_fact(con, "就业", "我", "离职于 2025-03-05",
                         evidence=ids + [999999], origin="derived")
    results.append(("证据必须指向真实消息", r.get("ok") and 999999 not in r["evidence"],
                    f"证据={r.get('evidence')}"))

    confirmed = profile.add_fact(con, "就业", "我", "现司：戊市，激光加工",
                                 evidence=ids[:2], origin="confirmed")
    r = profile.add_fact(con, "就业", "我", "现司：某半导体厂",
                         evidence=ids[:2], origin="derived")
    results.append(("推断不得覆盖已确认", r.get("ok") is False and "不得覆盖" in r.get("reason", ""),
                    f"返回已确认值={r.get('confirmed', {}).get('value')}"))

    newer = profile.add_fact(con, "就业", "我", "现司：戊市（转正）",
                             evidence=ids[:2], origin="confirmed")
    hist = profile.history(con, "就业", "我")
    old_gone = [h for h in hist if h["id"] == confirmed["id"]]
    results.append(("旧值不删、被串起来",
                    bool(old_gone) and old_gone[0]["superseded_by"] == newer["id"],
                    f"history {len(hist)} 条，旧值指向 #{newer['id']}"))

    active = profile.list_facts(con, subject="我")
    results.append(("list 只显示当前有效",
                    all(a["id"] != confirmed["id"] for a in active),
                    f"当前 {len(active)} 条"))

    # 人读视图：导出成 md。表是真相、文件是视图——导出之后文件里必须能读到断言原文。
    out_dir = cfg.path("paths", "db").parent / "_profile_md"
    exp = profile.export_md(con, cfg, out_dir=out_dir)
    body = ""
    p = out_dir / "就业.md"
    if p.exists():
        body = p.read_text(encoding="utf-8")
    results.append(("能导出人读视图（md）",
                    exp["ok"] and "就业.md" in exp["files"] and "转正" in body,
                    f"{exp['facts']} 条 → {','.join(exp['files'])}"))

    # 实体问题进队列：让"老板"提到 3 段（达到入选门槛），才真正测到入队与幂等
    tagging.add_tag(con, "老板", dimension="人物", state="approved")
    con.execute("UPDATE tags SET state='approved' WHERE dimension IN ('人物','机构')")
    msgs = tagging._conversation_messages(con, cid)
    tagging.apply_segments(con, cid, msgs, {"segments": [
        {"from": 1, "to": 1, "title": "吐槽老板 a",
         "tags": [{"dim": "人物", "name": "老板"}]},
        {"from": 2, "to": 2, "title": "吐槽老板 b",
         "tags": [{"dim": "人物", "name": "老板"}]},
        {"from": 3, "to": 3, "title": "吐槽老板 c",
         "tags": [{"dim": "人物", "name": "老板"}]}]})
    entities.sync_from_tags(con)
    before = len(review.list_pending(con, status="open", limit=100))

    n1 = 0
    for u in entities.unresolved_roles(con, limit=20):
        nid = review.add_pending(con, "entity", {
            "source": "entity", "sig_key": f"entity:{u['entity_id']}",
            "entity_id": u["entity_id"], "topic": f"「{u['name']}」",
            "old": u["why"], "new": "", "why": "是同一个人吗？"})
        n1 += 1 if nid else 0
    n2 = 0
    for u in entities.unresolved_roles(con, limit=20):
        nid = review.add_pending(con, "entity", {
            "source": "entity", "sig_key": f"entity:{u['entity_id']}",
            "entity_id": u["entity_id"], "topic": f"「{u['name']}」",
            "old": u["why"], "new": "", "why": "是同一个人吗？"})
        n2 += 1 if nid else 0
    results.append(("实体问题进队列且幂等", n1 >= 1 and n2 == 0,
                    f"首次 +{n1}，重复 +{n2}，原有 {before} 条"))

    con.close()
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)
    # 导出的 md 也清掉，别在 data/ 里留垃圾
    if out_dir.exists():
        for f in out_dir.glob("*.md"):
            f.unlink(missing_ok=True)
        try:
            out_dir.rmdir()
        except OSError:
            pass

    width = max(len(r[0]) for r in results)
    ok_all = True
    print("用户状态 + 实体问题进队列\n")
    for name, ok, detail in results:
        if not ok:
            ok_all = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
