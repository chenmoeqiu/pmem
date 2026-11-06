"""自动推断验收（不调 API，用假 provider）。

要守的边界：
1. 只写 origin='derived'，并带上**真实存在的**消息 id 作为证据
2. 没有证据的推断直接丢（不写"无据断言"）
3. 已确认的区间不被推断覆盖
4. 重复跑不叠加（按 org + 起止去重）
5. dry 不写库

用法：python tools/smoke_infer.py
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

from pmem import clock, db, entities, infer, store  # noqa: E402
from pmem.config import load  # noqa: E402


class FakeInfer:
    """按消息 id 编一个"前司 / 现司"两段经历，其中一条故意不带证据。"""
    name = "stub"

    def __init__(self, ids: list[int]):
        self.ids = ids

    def tag_batch(self, messages, *, max_tokens: int = 900):
        a, b = self.ids[0], self.ids[1]
        return (json.dumps({"episodes": [
            {"org": "前司", "role": "工艺工程师",
             "from": "2024-06", "to": "2025-03-05",
             "evidence": [a], "basis": "我在3月5日离职"},
            {"org": "现司", "role": "研发",
             "from": "2026-01", "to": "",
             "evidence": [b], "basis": "现在在戊市做激光加工"},
            {"org": "瞎编公司", "role": "", "from": "", "to": "",
             "evidence": [], "basis": "没有出处"},
        ]}, ensure_ascii=False),
            {"model": "stub", "tokens_in": 0, "tokens_out": 0})


def main() -> int:
    cfg = load()
    workdir = cfg.path("paths", "db").parent
    tmp = workdir / "_infer_smoke.db"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)
    con = db.open_db(tmp, force=True)

    cid = store.upsert_conversation(con, source="live", source_id="inf-smoke",
                                    title="推断验收", started_at=clock.now_utc_iso(),
                                    time_precision="message")
    ids = []
    for t in ("我在3月5日离职了", "现在在戊市做激光加工", "前司是做外延的"):
        ids.append(store.add_message(con, conversation_id=cid, role="user", origin="user",
                                     content=t, created_at=clock.now_utc_iso(),
                                     time_precision="message", source="live"))

    prov = FakeInfer(ids)
    results: list[tuple[str, bool, str]] = []

    # 1. dry 不写库
    d = infer.run(con, cfg, prov, limit=10, dry=True)
    n0 = con.execute("SELECT COUNT(*) FROM entity_tenure").fetchone()[0]
    results.append(("dry 不写库", d["written"] >= 1 and n0 == 0,
                    f"识别 {d['written']} 条，库里 {n0} 条"))

    # 2. 真跑：写 derived + 带证据
    r = infer.run(con, cfg, prov, limit=10)
    rows = con.execute(
        """SELECT t.origin, t.evidence, o.name FROM entity_tenure t
             LEFT JOIN entities o ON o.id = t.org_id ORDER BY t.id""").fetchall()
    ok = (r["written"] == 2 and all(x["origin"] == "derived" for x in rows)
          and all(x["evidence"] and x["evidence"] != "[]" for x in rows))
    results.append(("只写 derived 且带证据", ok,
                    f"写入 {r['written']} 条，证据 {[x['evidence'] for x in rows]}"))

    # 3. 无证据的被丢
    results.append(("无证据的推断被丢弃", r["no_evidence"] == 1,
                    f"丢 {r['no_evidence']} 条（瞎编公司）"))

    # 4. 重复跑不叠加
    r2 = infer.run(con, cfg, prov, limit=10)
    n2 = con.execute("SELECT COUNT(*) FROM entity_tenure").fetchone()[0]
    results.append(("重复跑不叠加", r2["written"] == 0 and n2 == 2,
                    f"第二次写入 {r2['written']}，总数 {n2}"))

    # 5. 用 API 把一条标成 confirmed，再推断一次：不得覆盖
    tid = con.execute(
        "SELECT id FROM entity_tenure WHERE origin='derived' ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    entities.confirm_tenure(con, tid)
    r3 = infer.run(con, cfg, prov, limit=10)
    conf = con.execute(
        "SELECT COUNT(*) FROM entity_tenure WHERE origin='confirmed'").fetchone()[0]
    kept = con.execute("SELECT origin FROM entity_tenure WHERE id = ?",
                       (tid,)).fetchone()["origin"]
    results.append(("确认后不被推断覆盖",
                    conf == 1 and kept == "confirmed" and r3["skipped_confirmed"] >= 1,
                    f"确认 {conf} 条，跳过确认 {r3['skipped_confirmed']} 次"))

    # 6. 依据（basis）落库，人才好逐条确认
    bases = [r["basis"] for r in con.execute(
        "SELECT basis FROM entity_tenure WHERE basis IS NOT NULL")]
    results.append(("原话依据落库", any("离职" in (b or "") for b in bases),
                    f"{len(bases)} 条带依据"))

    con.close()
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    width = max(len(r[0]) for r in results)
    ok_all = True
    print("自动推断（任职经历）\n")
    for name, ok, detail in results:
        if not ok:
            ok_all = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
