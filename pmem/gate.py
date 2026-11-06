"""证据门：检索之后、作答之前，判定"这些证据够不够回答"。

为什么需要它：
没有门的时候，"命中 3 条但不足以回答"会被静默当成"有证据"，于是模型
硬着头皮答——这正是幻觉的来源之一，而且比"没找到"更难发现。

为什么用代码强制：
prompt 说"不够就说不够"是没用的，模型倾向于把话圆回来。所以：
- sufficient=true 却给不出 used -> 代码判不足
- sufficient=true 但 next 不是 answer -> 代码判不足
- used 里引用了本批次不存在的编号 -> 剔除，剔空则判不足

另外它顺带解决了"证据利用率"：作答时只拿到 used 里的那几条，
所以不存在"召回了 5 条只用 2 条"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import prompt

NEXT_HINTS = {
    "search_keyword": "换一个说法或补充一个更具体的名字，我再查一次。",
    "ask_user": "需要你补充一点信息（时间、对象或具体名称）才能判断。",
    "stop": "这条我确实没有记录。",
    "answer": "",
}


@dataclass
class Verdict:
    sufficient: bool = False
    used: list[int] = field(default_factory=list)
    missing: str = ""
    next_action: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)
    tokens_in: int | None = None
    tokens_out: int | None = None

    def hint(self) -> str:
        return NEXT_HINTS.get(self.next_action, "")


def build_messages(question: str, hits: list[dict], snippet_chars: int = 220,
                   now_line: str = "") -> list[dict]:
    blocks = []
    for i, h in enumerate(hits, 1):
        when = h.get("created_at_local", "未知时间")
        title = (h.get("conversation_title") or "").replace("\n", " ")[:40]
        body = (h.get("content") or "").replace("\n", " ")
        if len(body) > snippet_chars:
            body = body[:snippet_chars] + "…"
        blocks.append(f"[{i}] {when}《{title}》\n{body}")
    evidence = "\n\n".join(blocks) if blocks else "（无）"
    head = f"【{now_line}】\n\n" if now_line else ""

    return [
        {"role": "system", "content": prompt.GATE_SYSTEM},
        {"role": "user", "content":
            f"{head}【用户问题】\n{question}\n\n【证据】\n{evidence}"},
    ]


def validate(data: dict | None, n_hits: int) -> Verdict:
    """把模型的判定过一遍代码规则。不合格就打回，不给它圆场的机会。"""
    if not data:
        return Verdict(sufficient=False, used=[], missing="证据判定失败",
                       next_action="stop", raw={})

    sufficient = bool(data.get("sufficient"))
    next_action = str(data.get("next") or "").strip().lower()
    missing = str(data.get("missing") or "").strip()[:160]

    used: list[int] = []
    raw_used = data.get("used")
    if isinstance(raw_used, list):
        for x in raw_used:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= n_hits and i not in used:
                used.append(i)
    used.sort()

    v = Verdict(sufficient=sufficient, used=used, missing=missing,
                next_action=next_action or "answer", raw=data or {})

    if sufficient:
        if not used:
            v.sufficient = False
            v.next_action = "search_keyword"
            v.missing = v.missing or "声称证据充足，但没有指出任何一条实际用到的证据"
        elif next_action and next_action != "answer":
            v.sufficient = False
    else:
        if v.next_action == "answer":
            v.next_action = "stop"
        if not v.missing:
            v.missing = "证据不足以回答"
    return v
