"""意图路由的对照实验：独立路由器 vs agent 自主决定。

两组测同一批输入，只比一件事——**该不该去查历史**。

A 组（我们现在的做法）：route.decide() 输出 need_recall
B 组（agent 做法）：给模型一个 search_memory 工具，看它自己要不要调用

注意样本是我自建的，带有我自己的判断偏向，不是金标准。
要让它有说服力，应该往 CASES 里换成你自己的真实输入和你的标注。

用法：python tools/eval_router.py
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

from pmem import llm, route  # noqa: E402
from pmem.config import load  # noqa: E402

# (输入, 该不该查历史)
# 上半部分：需要回溯（问的是"过去说过什么/我怎么想"）
# 下半部分：不需要回溯（当下任务、通用知识、随口一句）
CASES: list[tuple[str, bool]] = [
    ("我上次对000000是怎么说的", True),
    ("我之前是不是说过不喝手冲咖啡", True),
    ("我对甲省行程是怎么安排的", True),
    ("上次那个影之刃零的评价", True),
    ("我之前说过的机械键盘是什么轴", True),
    ("我去年买的那瓶八百多的酒", True),
    ("记得我说过要去珠三角吗", True),
    ("我之前提过的那个科幻小说idea", True),
    ("上次聊的eSIM那件事", True),
    ("我对加班是什么态度来着", True),

    ("今天有点累", False),
    ("帮我写一段 Python 读取 CSV 的代码", False),
    ("解释一下什么是 FTS5", False),
    ("帮我翻译这句话：hello world", False),
    ("推荐几本科幻小说", False),
    ("1+1 等于几", False),
    ("怎么在 Windows 上禁用迅雷下载", False),
    ("帮我把这段文字改成更正式的表述：明天开会", False),
    ("氢氧化钠的摩尔质量是多少", False),
    ("给我讲讲量子纠缠", False),
]

# 硬样本：模糊、指代、隐含需要历史。区分度都在这一组。
# 注意第 11–14 条是一个陷阱：指代的是"当前这场对话"，不是历史。
# 正确行为是**不检索**（热窗已经覆盖），检索了反而会把旧内容拉进来。
HARD_CASES: list[tuple[str, bool]] = [
    ("那件事怎么样了", True),
    ("继续", True),
    ("小鹏现在还能买吗", True),
    ("我这周有什么安排", True),
    ("你觉得我该去吗", True),
    ("帮我看看这个还值不值", True),
    ("上次说的那个方案定下来了吗", True),
    ("他在那个项目里负责什么", True),
    ("我是不是该换工作了", True),
    ("这个价格合理吗", True),

    ("帮我把上面那段话总结成三条", False),
    ("再写一个更短的版本", False),
    ("把这个改得口语一点", False),
    ("为什么天空是蓝色的", False),
]

# 留出样本：写完之后**不许再据此调规则**。只用来检查泛化，不是用来优化。
HOLDOUT: list[tuple[str, bool]] = [
    ("现在是不是买入机会", True),
    ("这个岗位适合我吗", True),
    ("我该选哪个", True),
    ("解释一下什么是电容", False),
    ("帮我把这段话翻译成英文", False),
    ("己市有哪些值得去的博物馆", False),
]

SEARCH_TOOL = [{
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": "检索用户过去对话的历史记录。只在回答需要用户个人历史时才调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {"type": "array", "items": {"type": "string"},
                             "description": "检索关键词"},
            },
            "required": ["keywords"],
        },
    },
}]

AGENT_SYSTEM = (
    "你在回答用户。你可以调用 search_memory 检索用户过去对话的历史记录。"
    "只有当你认为回答需要用户的个人历史（过去说过什么、当时的想法、之前的安排）时才调用它。"
    "当下任务、通用知识、闲聊不要调用。"
)


def arm_router(provider, text: str) -> bool:
    d = route.decide(provider, text, [])
    return d.need_recall


def arm_agent(provider, text: str) -> bool:
    msgs = [{"role": "system", "content": AGENT_SYSTEM},
            {"role": "user", "content": text}]
    msg, _ = provider.chat_with_tools(msgs, SEARCH_TOOL)
    calls = msg.get("tool_calls") or []
    return any((c.get("function") or {}).get("name") == "search_memory" for c in calls)


def score(pairs: list[tuple[bool, bool]]) -> dict:
    tp = sum(1 for t, p in pairs if t and p)
    tn = sum(1 for t, p in pairs if not t and not p)
    fp = sum(1 for t, p in pairs if not t and p)   # 误查
    fn = sum(1 for t, p in pairs if t and not p)   # 漏查
    n = len(pairs)
    return {
        "准确率": round((tp + tn) / n, 3),
        "漏查率": round(fn / max(1, tp + fn), 3),
        "误查率": round(fp / max(1, tn + fp), 3),
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
    }


def run_group(name: str, cases: list[tuple[str, bool]], provider) -> tuple[dict, dict, list]:
    print(f"\n### {name}（{len(cases)} 条）")
    a_pairs, b_pairs, rows = [], [], []
    for text, label in cases:
        a = arm_router(provider, text)
        b = arm_agent(provider, text)
        a_pairs.append((label, a))
        b_pairs.append((label, b))
        ok_a = "✓" if a == label else "✗"
        ok_b = "✓" if b == label else "✗"
        rows.append((text, label, a, b, ok_a, ok_b))
        print(f"  {ok_a} A  {ok_b} B   标={int(label)} A={int(a)} B={int(b)}  {text}")

    sa, sb = score(a_pairs), score(b_pairs)
    print(f"\n  {'指标':<8}{'A 独立路由':>14}{'B agent 自主':>14}")
    for k in ("准确率", "漏查率", "误查率"):
        print(f"  {k:<8}{sa[k]:>14}{sb[k]:>14}")
    wrong_a = [(t, int(l), int(a)) for t, l, a, _b, ma, _mb in rows if ma == "✗"]
    wrong_b = [(t, int(l), int(b)) for t, l, _a, b, _ma, mb in rows if mb == "✗"]
    if wrong_a:
        print("  A 错：", wrong_a)
    if wrong_b:
        print("  B 错：", wrong_b)
    return sa, sb, rows


def main() -> int:
    cfg = load()
    provider = llm.build(cfg)
    if provider.name == "offline":
        print("需要 API key 才能跑这个实验。")
        return 1

    run_group("简单样本", CASES, provider)
    run_group("硬样本（模糊/指代/隐含）", HARD_CASES, provider)
    run_group("留出样本（未参与调优）", HOLDOUT, provider)
    print("\n提示：样本是我自建的，带我的判断偏向；换成你的真实输入结果才有说服力。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
