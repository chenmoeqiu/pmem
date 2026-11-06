"""路由：决定"要不要回忆"。

三种来源，按可靠性排：
1. rule    规则下限保护——出现"上次/之前/记得"这类明确信号，模型说不用查也要查
2. model   模型判定——领域、关键词、歧义
3. offline 无 key 时的纯规则

规则不是用来省调用的，是用来兜住模型漏判的。延迟你已明确可以接受，
所以这里优先准确率而不是省一次请求。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from . import clock

# 比对关键词是否出自原话时用：忽略大小写、空格、连字符等
_NORM_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]")


def _canon(s: str) -> str:
    return _NORM_RE.sub("", (s or "").lower())

# 明确的"回溯"信号：命中就强制 need_recall
RECALL_HINTS = (
    "上次", "上一次", "之前", "以前", "我说过", "我跟你说", "记得", "还记得",
    "那次", "那件事", "刚才说", "继续那个", "前面提到", "前几天", "上周",
    "上个月", "去年", "历史", "回顾", "当时",
)
# "工作"信号：只在没有模型可用时作为兜底
WORK_HINTS = (
    "论文", "代码", "报错", "调试", "调研", "评估", "实现", "架构", "方案",
    "写一个", "改一下", "优化", "分析", "计算", "推导", "sql", "python", "api",
)
# 指代不明信号：只在"要回溯但没有关键词"时才判为歧义
VAGUE_HINTS = ("那件事", "那个事", "就那个", "那个东西", "那玩意儿", "这件事", "那个")

# 对"当前这段输出"的操作指令：这类输入要的是改写/压缩/翻译眼前的内容，
# 不是回溯历史。对照实验里 A 组的误查 100% 集中在这个模式上。
CURRENT_TURN_OPS = (
    "更短", "更长", "改写", "润色", "精简", "扩写", "压缩成",
    "总结成", "翻译成", "口语一点", "正式一点", "换个说法", "换一种说法",
)

# 触发第三档复核的信号：这句话在问"我的处境/我的决定"，但没有任何时间词。
# 只在"第一档判了不回溯"时才触发，所以不会增加正常对话的开销。
ESCALATE_HINTS = (
    "该不该", "是不是该", "值不值", "还能不能", "还能", "换不换",
    "要不要", "合不合适", "值得买", "该买", "该卖", "该去", "该留",
    "现在能", "现在还",
)


@dataclass
class Decision:
    domain: str = "casual"
    need_recall: bool = False
    keywords: list[str] = field(default_factory=list)
    ambiguous: bool = False
    source: str = "rule"
    reason: str = ""
    latency_ms: int = 0
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    escalated: bool = False
    # 从已批准词表里选出的标签：标签是检索主路径，全文只是兜底
    tags: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {"domain": self.domain, "need_recall": self.need_recall,
             "keywords": self.keywords, "ambiguous": self.ambiguous,
             "tags": self.tags},
            ensure_ascii=False,
        )


def _norm_tags(v: Any, vocab: list[str] | None) -> list[str]:
    """只保留**在已批准词表里**的标签，最多 3 个。模型爱编，编的就丢。"""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    allowed = set(vocab or [])
    out: list[str] = []
    for x in v:
        s = str(x).strip()
        if not s or s in out:
            continue
        if allowed and s not in allowed:
            continue
        out.append(s)
        if len(out) >= 3:
            break
    return out


def _literal_tags(text: str, vocab: list[str] | None) -> list[str]:
    """离线/兜底用：用户这句话里直接出现的标签名（忽略大小写空格）。"""
    t = _canon(text)
    if not t:
        return []
    return [v for v in (vocab or []) if _canon(v) and _canon(v) in t][:3]


def _norm_domain(v: Any) -> str:
    s = str(v or "").strip().lower()
    if s in ("work", "工作", "专业", "task"):
        return "work"
    return "casual"


def _norm_keywords(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out, seen = [], set()
    for x in v:
        s = str(x).strip()
        if not s or len(s) > 40 or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= 4:
            break
    return out


def decide(provider, user_text: str, recent: list[dict],
           vocab: list[str] | None = None) -> Decision:
    """一次判定。provider 可以是 HttpProvider / OfflineProvider。

    `vocab` 是已批准的标签名。给了就让模型从中选标签——标签是检索主路径，
    "换个说法还能不能查到"就取决于这一步。
    """
    t0 = time.time()
    text = user_text or ""
    force_recall = any(w in text for w in RECALL_HINTS)
    rule_domain = "work" if any(w in text.lower() for w in WORK_HINTS) else "casual"
    literal_tags = _literal_tags(text, vocab)

    try:
        data, usage = provider.classify(text, recent, vocab)
    except Exception as e:  # 网络/格式问题一律降级到规则，不让对话断掉
        d = Decision(
            domain=rule_domain,
            need_recall=force_recall,
            keywords=[w for w in RECALL_HINTS if w in text][:3],
            ambiguous=force_recall and any(w in text for w in VAGUE_HINTS),
            source="rule",
            reason=f"模型不可用，退回规则：{type(e).__name__}",
            latency_ms=int((time.time() - t0) * 1000),
            tags=literal_tags,
        )
        return d

    d = Decision(
        domain=_norm_domain(data.get("domain")),
        need_recall=bool(data.get("need_recall")),
        keywords=_norm_keywords(data.get("keywords")),
        ambiguous=bool(data.get("ambiguous")),
        source=getattr(provider, "name", "model"),
        reason=str(data.get("reason") or "")[:120],
        latency_ms=int((time.time() - t0) * 1000),
        model=usage.get("model"),
        tokens_in=usage.get("tokens_in"),
        tokens_out=usage.get("tokens_out"),
        raw=data,
        tags=_norm_tags(data.get("tags"), vocab),
    )
    # 模型没给标签、但用户这句话里直接出现了标签名 → 用字面命中补上
    if not d.tags and literal_tags:
        d.tags = literal_tags
        d.reason = (d.reason + "｜标签按字面命中补全").strip("｜")

    # 规则下限：模型漏判"上次"这类硬信号时纠正它
    if force_recall and not d.need_recall:
        d.need_recall = True
        d.reason = (d.reason + "｜规则补正：命中回溯信号").strip("｜")

    # 关键词必须出自用户当前这句话。模型很容易从"最近对话"里搬词，
    # 那会导致一句"今天有点累"去检索上一轮提到的股票代码——正是过度联想。
    def _filter_keywords(words: list[str]) -> list[str]:
        t = _canon(text)
        kept = [k for k in words if _canon(k) and _canon(k) in t]
        if len(kept) != len(words):
            d.reason = (d.reason + "｜已剔除不在原话中的关键词").strip("｜")
        return kept

    if d.keywords:
        d.keywords = _filter_keywords(d.keywords)

    is_current_op = any(w in text for w in CURRENT_TURN_OPS) and not force_recall

    # 第三档复核：第一档判了"不回溯"时，用一次专门的问题再确认。
    # 触发条件刻意做成**结构性**的（"判了不回溯就复核"），而不是靠词表——
    # 实测过：词表式触发只在调优集上有效，换说法就漏（留出样本漏查 0.67）。
    if not d.need_recall and not is_current_op:
        try:
            sdata, susage = provider.reconsider(text, recent)
            d.model = susage.get("model") or d.model
            d.tokens_in = (d.tokens_in or 0) + (susage.get("tokens_in") or 0)
            d.tokens_out = (d.tokens_out or 0) + (susage.get("tokens_out") or 0)
            d.escalated = True
            if sdata.get("needs_history"):
                d.need_recall = True
                kws = _filter_keywords(_norm_keywords(sdata.get("keywords")))
                if kws:
                    d.keywords = kws
                d.reason = (d.reason + f"｜复核改判：{sdata.get('reason', '')}")[:200].strip("｜")
        except Exception as e:  # 复核失败不能阻断，退回第一档判定
            d.reason = (d.reason + f"｜复核失败：{type(e).__name__}")[:200].strip("｜")

    # 要回溯却拿不出任何可用关键词 => 无法检索，应该反问而不是硬猜
    if d.need_recall and not d.keywords:
        d.ambiguous = True

    # 对"当前这段输出"的改写/压缩/翻译请求：不回溯
    if is_current_op:
        if d.need_recall:
            d.reason = (d.reason + "｜操作对象是当前对话，不回溯").strip("｜")
        d.need_recall = False
        d.keywords = []
        d.ambiguous = False

    return d


def log_decision(
    con: sqlite3.Connection,
    *,
    conversation_id: int | None,
    user_text: str,
    d: Decision,
    hits: int,
    gate_sufficient: bool | None = None,
    gate_used: list[int] | None = None,
    gate_missing: str | None = None,
    tokens_gate_in: int | None = None,
) -> None:
    con.execute(
        """INSERT INTO routing_log
               (conversation_id, input_preview, domain, need_recall, keywords,
                ambiguous, source, reason, hits, latency_ms, model,
                tokens_in, tokens_out, created_at,
                gate_sufficient, gate_used, gate_missing, tokens_gate_in)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (conversation_id, user_text.replace("\n", " ")[:120], d.domain,
         int(d.need_recall), json.dumps(d.keywords, ensure_ascii=False),
         int(d.ambiguous), d.source, d.reason, hits, d.latency_ms,
         d.model, d.tokens_in, d.tokens_out, clock.now_utc_iso(),
         None if gate_sufficient is None else int(gate_sufficient),
         json.dumps(gate_used or [], ensure_ascii=False),
         gate_missing, tokens_gate_in),
    )


def recent_logs(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    off_row = con.execute(
        "SELECT * FROM routing_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in off_row:
        d = {k: r[k] for k in r.keys()}
        d["created_at_local"] = clock.local_str(d["created_at"])
        try:
            d["keywords"] = json.loads(d["keywords"] or "[]")
        except Exception:
            d["keywords"] = []
        out.append(d)
    return out
