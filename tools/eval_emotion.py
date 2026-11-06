"""情绪维度对照实验：现行提示词（A） vs 「只标自述」提示词（B）。

同一批会话、同一份词表，只改系统提示词里情绪维度的规则，比两个指标：
1. **被标「怀疑」的段占比** —— 现在全库是 320/807 = 40%，疑似把"提问语气"当成了情绪
2. 每段的情绪标签数

不写库、不改词表，纯对照。结果同时打印并写到 data/emotion_eval.txt。

用法：python tools/eval_emotion.py [--n 20]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pmem import db, llm, prompt, tagging  # noqa: E402
from pmem.config import load  # noqa: E402

# B 组追加的约束：情绪只记"用户自述的状态"，不记提问语气
EMOTION_STRICT = """
情绪维度的额外约束（覆盖上面所有与之冲突的说法）：
- 情绪只在**用户直接说出自己的身心状态**时才标：例如"今天有点累""我有点慌""最近很内耗"。
- 以下情况**一律不标情绪**：
  · 提问（"是不是可以卖了""这个值得吗""能升套房吗"）
  · 评价他人或作品、描述客观事实、讨论概念
  · **疑问语气不等于怀疑**："是不是…""会不会…""好像…"只是提问方式，不是情绪
- 一段话没有明确自述状态时，情绪维度就留空——**宁可不标，也不要猜**。
"""


def count(data: dict) -> dict:
    segs = [s for s in (data.get("segments") or []) if isinstance(s, dict)]
    emo = doubt = tags = 0
    for s in segs:
        ts = [t for t in (s.get("tags") or []) if isinstance(t, dict)]
        tags += len(ts)
        e = [t for t in ts if str(t.get("dim") or "").strip() == "情绪"]
        emo += len(e)
        if any(str(t.get("name") or "").strip() == "怀疑" for t in e):
            doubt += 1
    return {"segs": len(segs), "tags": tags, "emo": emo, "doubt": doubt}


def main() -> int:
    n = 20
    argv = sys.argv[1:]
    if "--n" in argv:
        n = int(argv[argv.index("--n") + 1])

    cfg = load()
    con = db.open_db(cfg.path("paths", "db"))
    provider = llm.build(cfg)
    if provider.name == "offline":
        print("需要 API key。")
        return 1

    vocab = tagging.vocabulary(con)
    ids = [int(r[0]) for r in con.execute(
        "SELECT id FROM conversations WHERE state = 'active' ORDER BY id")]
    step = max(1, len(ids) // n)
    sample = ids[::step][:n]

    agg = {"A": {"segs": 0, "tags": 0, "emo": 0, "doubt": 0},
           "B": {"segs": 0, "tags": 0, "emo": 0, "doubt": 0}}
    rows: list[tuple[int, dict, dict]] = []
    failed = 0

    for cid in sample:
        row = con.execute("SELECT title FROM conversations WHERE id = ?", (cid,)).fetchone()
        title = row["title"] or "" if row else ""
        msgs = tagging._conversation_messages(con, cid)
        if not msgs:
            continue
        # A 组 = 去掉情绪约束的原始提示词；B 组 = 现在生产用的提示词（含约束）
        full = prompt.build_segment_messages(title, msgs, vocab)
        bare = [{"role": "system", "content": prompt.SEGMENT_SYSTEM_BASE}, full[1]]
        try:
            ra, _ = provider.tag_batch(bare, max_tokens=2500)
            rb, _ = provider.tag_batch(full, max_tokens=2500)
            a = count(json.loads(ra) if isinstance(ra, str) else (ra or {}))
            b = count(json.loads(rb) if isinstance(rb, str) else (rb or {}))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  会话 {cid} 失败：{e}")
            continue
        rows.append((cid, a, b))
        for k, v in (("A", a), ("B", b)):
            for f in agg[k]:
                agg[k][f] += v[f]

    out = ["情绪维度对照实验（A=现行，B=只标自述）", ""]
    out.append(f"样本 {len(rows)} 场会话，失败 {failed} 场")
    out.append("")
    out.append("| 会话 | A 段/情绪/怀疑 | B 段/情绪/怀疑 |")
    out.append("|---|---|---|")
    for cid, a, b in rows:
        out.append(f"| {cid} | {a['segs']}/{a['emo']}/{a['doubt']} | "
                   f"{b['segs']}/{b['emo']}/{b['doubt']} |")
    out.append("")
    for k, label in (("A", "现行提示词"), ("B", "只标自述")):
        s, e, d = agg[k]["segs"], agg[k]["emo"], agg[k]["doubt"]
        rate = (d / s * 100) if s else 0
        per = (e / s) if s else 0
        out.append(f"{label}：段 {s}，情绪标签 {e}（{per:.2f} 个/段），"
                   f"含「怀疑」的段 {d}（{rate:.1f}%）")
    text = "\n".join(out)
    print(text)
    (cfg.root / "data" / "emotion_eval.txt").write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
