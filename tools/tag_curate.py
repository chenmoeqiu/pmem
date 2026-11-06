"""把一堆候选收成一套能入库的词表。

为什么不用模型再合并一次：模型合并会把"甲省"和"乙市"合成一类（它们是 is-a，
不是同义），也会把频次信息揉掉。所以收敛这一步用确定性规则：

1. **频次门槛**：count < min_count 的丢掉（只出现一次的词多半是偶发表述）
2. **包含归并**：同一维度内，若 A 是 B 的子串，保留频次高的、合并频次
3. **每维封顶**：按频次取前 cap 个
4. **输出**：一份 markdown + 一条可直接复制的 `pmem tag add` 命令

用法：python tools/tag_curate.py [--limit 600] [--min-count 2] [--cap 12]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pmem import db, llm, prompt, tagging  # noqa: E402
from pmem.config import load  # noqa: E402

DIMS = prompt.DIMENSIONS
# 这些值会把每条消息都打上，等于没信息量，直接丢
BLOCK = {"用户本人", "用户", "自己", "我", "其他", "杂项"}


def with_parents(final: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """父级如果不在集合里，补成结构节点（count=0，排在本维最前）。

    否则 `tag add --parent 甲省` 会因为甲省不存在而丢掉层级。
    """
    for dim, rows in final.items():
        have = {c["name"] for c in rows}
        missing: dict[str, dict] = {}
        for c in list(rows):
            p = (c.get("parent") or "").strip()
            if p and p not in have and p not in BLOCK:
                missing.setdefault(p, {"dim": dim, "name": p, "count": 0,
                                       "parent": "", "members": []})
        if missing:
            final[dim] = list(missing.values()) + rows

        # 拓扑排序：父级必须排在子级前面，否则 `tag add --parent` 解析不到
        rows2 = final[dim]
        for _ in range(6):
            moved = False
            for i, c in enumerate(rows2):
                p = (c.get("parent") or "").strip()
                if not p:
                    continue
                pos = next((j for j, x in enumerate(rows2) if x["name"] == p), None)
                if pos is not None and pos > i:
                    rows2[i], rows2[pos] = rows2[pos], rows2[i]
                    moved = True
            if not moved:
                break
        final[dim] = rows2
    return final


def curate(cands: list[dict], min_count: int, cap: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {d: [] for d in DIMS}
    for dim in DIMS:
        group = [c for c in cands
                 if c["dim"] == dim and c["count"] >= min_count
                 and c["name"] not in BLOCK]
        group.sort(key=lambda x: (-x["count"], -len(x["name"]), x["name"]))
        kept: list[dict] = []
        for c in group:
            dup = next((k for k in kept
                        if c["name"] in k["name"] or k["name"] in c["name"]), None)
            if dup is not None:
                dup["count"] += c["count"]
                continue
            kept.append(dict(c))
        kept.sort(key=lambda x: (-x["count"], x["name"]))
        out[dim] = kept[:cap]
    return out


def main() -> int:
    limit = 600
    min_count = 2
    cap = 12
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--limit":
            limit = int(args[i + 1])
        elif a == "--min-count":
            min_count = int(args[i + 1])
        elif a == "--cap":
            cap = int(args[i + 1])

    cfg = load()
    con = db.open_db(cfg.path("paths", "db"))
    provider = llm.build(cfg)
    if provider.name == "offline":
        print("需要 API key。")
        return 1

    r = tagging.propose(con, cfg, provider, limit=limit, batch_size=20)
    cands = r["candidates"]
    merged = tagging.consolidate(provider, cands) if len(cands) >= 6 else cands
    final = with_parents(curate(merged, min_count, cap))

    lines = ["# 词表提案", "",
             f"来源：最近 {r['scanned']} 条消息，{r['batches']} 批；"
             f"原始 {len(cands)} → 合并 {len(merged)} → 收敛后 "
             f"{sum(len(v) for v in final.values())} 个", ""]
    cmds: list[str] = []
    for dim in DIMS:
        rows = final[dim]
        if not rows:
            continue
        print(f"\n【{dim}】{len(rows)} 个")
        lines.append(f"## {dim}")
        for c in rows:
            parent = f"（父：{c['parent']}）" if c.get("parent") else ""
            print(f"  {c['count']:>3}  {c['name']}{parent}")
            lines.append(f"- {c['name']}（{c['count']} 次）{parent}")
            cmds.append(f'tag add "{c["name"]}" --dim {dim}'
                        + (f' --parent "{c["parent"]}"' if c.get("parent") else ""))
        lines.append("")

    # 输出到 docs/：词表来自真实消息（含真实地名/人名/机构名），
    # 这是**私有仓**的正当产物，要进版本历史。
    # 公开仓不复制这两份（那里由合成样例重新生成），见 docs/DESIGN.md 的说明。
    out = cfg.root / "docs" / "VOCAB_PROPOSAL.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    batch = cfg.root / "docs" / "vocab_add.cmd"
    # %~dp0 是 docs\，退一级就是项目根（run.cmd 在那儿）
    batch.write_text("@echo off\r\ncd /d %~dp0..\r\n"
                     + "\r\n".join(f"run.cmd {c}" for c in cmds), encoding="utf-8")
    print(f"\n合计 {sum(len(v) for v in final.values())} 个 → {out}")
    print(f"一条命令全部入库 → {batch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
