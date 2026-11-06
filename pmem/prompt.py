"""提示词与上下文组装。

这里承载了整套系统最重要的产品决策：
- 没检索到就说没找到，绝不补全
- 引用过去必须带时间
- 用户没提的话题，不许硬扯进来（正是 RAG 的病）
- 人设与语气可配（`persona.md`），但**不许盖过上面三条**
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import clock

# ---------------------------------------------------------------- 人设加载


def load_persona(cfg) -> str:
    """读用户自己的人设文件（默认 `persona.md`，可被 config 覆盖）。

    为什么放在文件里而不是配置里：人设通常几百字、要反复改，
    塞进一行 TOML 没法写多行文本；而且它属于**这个人**，不该进仓库
    （`.gitignore` 里有 `persona.md`，仓库里只留 `persona.example.md`）。
    """
    inline = str(cfg.get("chat", "persona", default="") or "").strip()
    if inline:
        return inline
    rel = str(cfg.get("chat", "persona_file", default="persona.md") or "").strip()
    if not rel:
        return ""
    p = Path(rel)
    if not p.is_absolute():
        p = cfg.root / p
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    return ""

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def current_time_line(utc_offset_hours: float = 8) -> str:
    """当前时间锚点。

    没有它，模型不知道"上个月""去年"是哪个月；有了它，相对时间才能落到
    具体日期上。它解决的是**时间解析**，解决不了"这句话到底要不要查历史"。
    """
    now = clock.parse_iso(clock.now_utc_iso())
    if now is None:
        return ""
    local = clock.local_str(clock.now_utc_iso(), utc_offset_hours)
    return f"当前时间：{local}（{_WEEKDAYS[now.weekday()]}，UTC+{utc_offset_hours:g}）"

# ---------------------------------------------------------------- 人设

SYSTEM_CHAT = """你是用户的本地个人记忆助手。你的全部"记忆"来自下面提供的记忆片段。

硬性规则：
1. 关于过去的问题，只能依据记忆片段回答。片段里没有的，直接说没找到，不要推测、不要补全、不要用"一般来说"替代。
2. 引用过去时带上时间（例如"2026-10-31 你说过……"）。
3. 用户没有提到的话题，不要主动牵扯进来。不要为了显得贴心而联想用户的其他偏好。
4. 区分"用户当时说过的事实"和"当时模型的建议"，不要把后者当成事实复述。
5. 语气客观、简洁。不说教，不推销。
6. **闲聊就正常聊**：不必每句话都把记忆搬出来，也不要因为"这段记忆里没提"就拒答日常问题；
   只有被问到过去的事，才需要回到规则 1。
7. 不要为"没记住"道歉，不要复述用户的问题，不要说"作为一个 AI"，不要加免责声明。
8. 长度跟着场景走：一句能说完就一句，别为了显得完整而铺开模板化清单。

如果记忆片段为空、或与问题无关，就用一句话说明你这边没有相关记录，并建议用户换一个说法或给出大致时间。"""

# 人设文件会被拼在系统提示词**最前面**：先定"你是谁、怎么说话"，
# 再讲记忆规则。顺序反过来，模型会把语气规则当成记忆规则的附注。
PERSONA_TEMPLATE = """【你是谁、怎么说话】
{persona}

（以上是语气与偏好。它不能覆盖下面的记忆规则：没有记录就说没有记录。）"""

SYSTEM_GATED = """你是用户的本地个人记忆助手。下面是**已经通过证据门审核**的记忆片段——每一条都必须用到。

硬性规则：
1. 只依据这些片段回答，不要补充片段之外的任何信息。
2. 每一条片段都要在回答里体现，不要挑其中一两条就用。
3. 引用过去时带上时间（例如"2026-10-31 你说过……"）。
4. 如果片段之间有时间差异或态度变化，要如实说明先后顺序。
5. 用户没有提到的话题，不要主动牵扯进来。
6. 语气客观、简洁。不说教，不推销。
7. 当用户要你**基于记忆做判断或建议**（例如"该不该送""这个还成立吗"），允许推导，
   但必须标明这是从哪几条证据推出来的（例如"按你 2026-10-04 的口味标准看……"），
   绝不能把自己的推断说成用户说过的话。"""

SYSTEM_TASK = """你在执行用户下发的一个任务。

规则：
1. 任务需要用户的历史信息时，只看下面提供的记忆片段；片段里没有的，
   明确说"这部分没有记录"，不要编造用户说过或做过什么。
2. 任务需要通用能力时（写模板、搭分析框架、列检查清单），直接用你的能力完成，
   **不要因为"记忆里没有"就拒绝整个任务**。
3. 产出要能直接用：给结论、给结构，不要寒暄。
4. 任务本身缺必要输入（没给公司名、没给文件）时，直接指出缺什么，不要假装做了。"""


def build_task_messages(goal: str, hits_text: str = "", now_line: str = "") -> list[dict]:
    parts: list[str] = []
    if now_line:
        parts.append(f"【{now_line}】")
    parts.append("【记忆片段】\n" + (hits_text or "（无）"))
    return [
        {"role": "system", "content": SYSTEM_TASK},
        {"role": "system", "content": "\n\n".join(parts)},
        {"role": "user", "content": f"任务：{goal}"},
    ]


SYSTEM_CLARIFY = """上一轮你没能确定用户指的是哪件事。现在你只做一件事：用一句话向用户确认。

要求：
- 直接给出 2-3 个候选，每个候选带时间，让用户挑
- 不要解释你为什么不确定
- 不要在确认之前回答原本的问题"""

# ---------------------------------------------------------------- 证据门

GATE_SYSTEM = """你是证据审核员。判断给定证据是否足以回答用户的问题。只输出 JSON，不要解释。

字段：
- "sufficient": 布尔值，证据是否足以回答
- "used": 整数数组，你实际会用到哪些证据（从 1 开始的编号）
- "missing": 字符串，缺什么信息（≤120 字；sufficient=true 时留空）
- "next": "answer" | "search_keyword" | "ask_user" | "stop"
    answer         = 证据够，可以作答
    search_keyword = 换一组关键词可能找到
    ask_user       = 需要用户补充信息（时间、对象、名称）
    stop           = 记忆里确实没有，直接说没有

判定纪律：
- 只能回答部分维度时判 false（例如问"为什么"，但证据里只有"是什么"）
- sufficient=true 时 used 必须非空，且 next 必须是 answer
- 宁可判不足，也不要让回答者去猜

不要过度保守：
- 问题里有"上次/之前"这类指代，而证据有多条相关记录时——
  只要这些记录一起能给出有意义的回答（例如按时间把它们列出来），就判充足。
  不要因为"无法确定唯一是哪一次"而判不足，也不要把这类情况判成 ask_user。
- 证据能支撑"部分回答 + 说清边界"时，判充足，由回答者去标注边界。"""

SECOND_OPINION_SYSTEM = """你是回溯复核员。第一道判定认为"这句话不需要查历史"，
但你只回答一个问题：**要给出有用的回答，需要知道用户的个人处境吗？**

用户处境包括：有没有持仓、之前的计划、预算、偏好、已做的决定、正在做的事。

判断依据：
- 股票/买卖/该不该/值不值/还能不能 这类问题，往往需要知道用户已有仓位与既定计划
- 纯技术问题、通用知识、翻译改写，不需要
- 这句话里没有任何时间词（没有"上次/之前"），所以你要判断的是**隐含的**依赖

字段：
- "needs_history": 布尔
- "keywords": 字符串数组，检索历史用的实体（needs_history=true 时给 1-3 个，取自原话）
- "reason": 一句话

拿不准时判 true —— 多查一次的成本，远低于答错的成本。只输出 JSON。"""


CONSOLIDATE_SYSTEM_PLACEHOLDER = None


def build_consolidate_messages(candidates: list[dict]) -> list[dict]:
    lines = [f"- [{c['dim']}] {c['name']}（{c['count']} 次）" for c in candidates]
    return [
        {"role": "system", "content": CONSOLIDATE_SYSTEM},
        {"role": "user", "content": "【候选值】\n" + "\n".join(lines)},
    ]


DIMENSIONS = ("主题", "地点", "人物", "机构", "任务", "情绪")

SEGMENT_SYSTEM_BASE = """你在给一场对话打标。两步：先按话题切段，再给每段打多维标签。

切分规则：
- 话题明显切换处切一刀；同一话题连聊多轮就并成一段
- 每段至少 1 条消息，不许空段，段的区间不许重叠
- **宁少勿多**：一场对话通常 1-3 段，只有真换了话题才切

标签规则：
- 维度只能是：主题 / 地点 / 人物 / 机构 / 任务 / 情绪
- 每个值优先从【允许的词表】里选，**逐字一致**
- 词表里没有合适的，就给一个新值（2-8 字），并在 parent 里写它的父级名字（可留空）
- 每段 2-5 个标签，其中至少要有一个"主题"

只输出 JSON：
{"segments":[{"from":1,"to":6,"title":"甲省行程规划","reason":"这里从行程切到酒店价格",
"tags":[{"dim":"地点","name":"甲省","parent":"中国"},{"dim":"主题","name":"旅游"},
{"dim":"任务","name":"行程规划"}]}]}"""

# 情绪维度：只标"用户自述的身心状态"。
#
# 为什么加这条（实测，不是拍的）：不加时全库 807 段里有 320 段被挂"怀疑"（40%），
# 抽样看全是被读成情绪的**提问语气**——"是不是可以结账了""能升套房吗""是什么档次"。
# 20 场会话对照实验（tools/eval_emotion.py）：加上后「怀疑」段占比 50% → 3.3%，
# 且仍能抓到真自述（"我有点搞不明白了"→ 情绪/内耗），不是一律不标。
EMOTION_RULE = """

情绪维度的额外约束（覆盖上面所有与之冲突的说法）：
- 情绪只在**用户直接说出自己的身心状态**时才标：例如"今天有点累""我有点慌""最近很内耗"。
- 以下情况**一律不标情绪**：
  · 提问（"是不是可以卖了""这个值得吗""能升套房吗"）
  · 评价他人或作品、描述客观事实、讨论概念
  · **疑问语气不等于怀疑**："是不是…""会不会…""好像…"只是提问方式，不是情绪
- 一段话没有明确自述状态时，情绪维度就留空——**宁可不标，也不要猜**。
"""

SEGMENT_SYSTEM = SEGMENT_SYSTEM_BASE + EMOTION_RULE


def build_segment_messages(conversation_title: str, messages: list[dict],
                           vocab: list[dict]) -> list[dict]:
    """messages: [{seq, role, content}]，按 seq 升序。"""
    by_dim: dict[str, list[str]] = {}
    for t in vocab:
        by_dim.setdefault(t["dimension"], []).append(t["name"])
    vocab_text = "\n".join(f"- {d}：{'、'.join(v)}" for d, v in by_dim.items()) or "（空）"

    lines = []
    for m in messages:
        who = "用户" if m["role"] == "user" else "助手"
        body = (m["content"] or "").replace("\n", " ")
        if len(body) > 220:
            body = body[:220] + "…"
        lines.append(f"[{m['seq']}] {who}: {body}")

    return [
        {"role": "system", "content": SEGMENT_SYSTEM},
        {"role": "user", "content":
            f"【对话标题】{conversation_title}\n\n【允许的词表】\n{vocab_text}\n\n"
            f"【消息】（方括号里是序号，from/to 用它）\n" + "\n".join(lines)},
    ]


PROPOSE_SYSTEM = """你在归纳一个用户的领域词表。

读下面的消息，给出它们涉及的"值"，并标出维度。

规则：
- 维度只能是：主题 / 地点 / 人物 / 机构 / 任务 / 情绪
- 每个值 2-8 个字，名词短语，必须能在消息里对上
- **尽量具体**，不要"生活""其他"这类没有信息量的词
- 能给父级就给（如 甲省 → 中国），给不出就留空
- 每批最多 16 个

只输出 JSON：
{"items":[{"dim":"地点","name":"甲省","parent":"中国"},
{"dim":"主题","name":"半导体工艺","parent":""}]}"""


def build_propose_messages(messages: list[dict]) -> list[dict]:
    blocks = []
    for i, m in enumerate(messages, 1):
        body = (m["content"] or "").replace("\n", " ")
        if len(body) > 240:
            body = body[:240] + "…"
        blocks.append(f"[{i}] {body}")
    return [
        {"role": "system", "content": PROPOSE_SYSTEM},
        {"role": "user", "content": "【消息】\n" + "\n".join(blocks)},
    ]


CONSOLIDATE_SYSTEM = """下面是从同一个用户真实对话里归纳出的候选值（带维度）。
问题是同一件事被起了不同名字（如"甲省""甲省""乙市"可归到同一支）。

请合并成一套紧凑词表。

规则：
- 维度不许改；只在同维度内合并
- 合并后的名字 2-8 字，必须是输入里逐字出现过的
- members 写被合并掉的原始名字（逐字）
- 不要"其他""杂项"这类空维度值
- 每条给出 parent（父级名字，可为空）

只输出 JSON：
{"categories":[{"dim":"地点","name":"甲省","parent":"中国","members":["甲省","甲省"]}]}"""


TAGGER_SYSTEM = """你是打标员。给每条用户消息打 1-3 个标签。

规则：
1. **只能从【允许的标签】里选**。自己改个说法不算数——必须是列表里逐字一致的名字。
2. 只有确实没有任何标签能表达时，才提议新标签：整批最多 2 个，名字 2-8 个字，
   并在 reason 里说明为什么现有标签不够。
3. 标签描述"这段内容在讲什么类别"，不要评价情绪、不要下判断、不要写整句。
4. 每条消息至少给 1 个标签。**不要为了省事用"其他"**——"其他"是最后手段：
   如果现有标签匹配不上，宁可提议一个新标签（见规则 2）。

只输出 JSON，不要解释：
{"items":[{"id":1,"tags":["投资"],"new_tags":[{"name":"半导体工艺","kind":"topic"}],"reason":""}]}"""


def build_tagging_messages(messages: list[dict], allowed: list[dict], total_allowed: int) -> list[dict]:
    """messages: [{id, content}]；allowed: [{name, kind}]（闭集词表）"""
    vocab = "、".join(f"{t['name']}（{t['kind']}）" for t in allowed) or "（空）"
    blocks = []
    for i, m in enumerate(messages, 1):
        body = (m["content"] or "").replace("\n", " ")
        if len(body) > 400:
            body = body[:400] + "…"
        blocks.append(f"[{i}] {body}")
    return [
        {"role": "system", "content": TAGGER_SYSTEM},
        {"role": "user", "content":
            f"【允许的标签】（共 {total_allowed} 个，只能用这些）\n{vocab}\n\n"
            f"【待打标消息】\n" + "\n".join(blocks)},
    ]


def build_second_opinion_messages(
    user_text: str, recent: list[dict], limit: int = 4, now_line: str = ""
) -> list[dict]:
    lines = []
    for m in recent[-limit:]:
        who = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"{who}: {(m.get('content') or '').replace(chr(10), ' ')[:100]}")
    block = "\n".join(lines) if lines else "（无）"
    head = f"【{now_line}】\n\n" if now_line else ""
    return [
        {"role": "system", "content": SECOND_OPINION_SYSTEM},
        {"role": "user", "content":
            f"{head}【最近对话】\n{block}\n\n【用户当前输入】\n{user_text}"},
    ]


ROUTER_SYSTEM = """你是一个意图路由器。只输出 JSON，不要任何解释或代码围栏。

判断用户这句话，输出以下字段：

- "domain": "work" 或 "casual"
  work   = 需要专业产出或执行：论文、代码、技术方案、数据分析、正式文书、评估某种技术
  casual = 聊天、情绪、生活琐事、随口提问、发图讨论感受
- "need_recall": 布尔值，是否需要回溯历史对话才能回答
  明确提到过去（上次/之前/记得/那件事）或需要用户的历史状态（行程、买了没、说过什么）时为 true
- "keywords": 字符串数组，用于检索历史，2-4 个
  必须**出自【用户当前输入】**，不要从【最近对话】里搬词；
  优先实体、专有名词、型号、编号、人名、地名；没有明确实体就给空数组
  禁止把"上次""之前""那件事"这类虚词当作关键词
- "ambiguous": 布尔值，指代是否不明（例如只说"那件事""就那个"而没有具体对象）
- "tags": 字符串数组，0-3 个，**从【可选标签】里逐字选**，表示这句话涉及哪些标签。
  用户常用同义词或指代（"那家光学公司"→"某激光公司"、"那个股票"→"某航运公司"），
  这时靠标签检索，字面词是查不到的。跟任何标签都无关、或【可选标签】为空时给空数组，
  **不要凭猜测硬塞**。
- "reason": 一句话理由

只输出 JSON。"""


def build_router_messages(
    user_text: str, recent: list[dict], limit: int = 6, now_line: str = "",
    vocab: list[str] | None = None,
) -> list[dict]:
    """路由器只看最近几轮，用来消解代词（"他""那个"指谁）。

    `vocab` 是已批准标签名列表：给了就让模型从中选 `tags`，
    标签是检索的主路径（全文只是兜底），所以这一步决定"换个说法还能不能查到"。
    """
    lines = []
    for m in recent[-limit:]:
        who = "用户" if m.get("role") == "user" else "助手"
        snippet = (m.get("content") or "").replace("\n", " ")[:120]
        lines.append(f"{who}: {snippet}")
    recent_block = "\n".join(lines) if lines else "（无）"

    head = f"【{now_line}】\n\n" if now_line else ""
    vocab_block = ""
    if vocab:
        vocab_block = f"【可选标签】（只能从这里逐字选）\n{'、'.join(vocab)}\n\n"
    return [
        {"role": "system", "content": ROUTER_SYSTEM},
        {"role": "user", "content":
            f"{head}{vocab_block}【最近对话】\n{recent_block}\n\n【用户当前输入】\n{user_text}"},
    ]


# ---------------------------------------------------------------- 预算

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """粗略估算：汉字约 1 token/字，西文约 3.5 字符/token。偏保守。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return int(cjk + (len(text) - cjk) / 3.5) + 1


def _clip(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    # 按字符比例粗裁，再补省略号
    ratio = max_tokens / max(1, estimate_tokens(text))
    cut = max(40, int(len(text) * ratio))
    return text[:cut].rstrip() + "…"


# ---------------------------------------------------------------- 上下文

def render_hot(hot: list[dict], budget: int) -> str:
    """热记忆：整段注入。

    `budget <= 0` 表示不截断——这是设计意图（热记忆 = 当前清醒段的全部文字）。
    若调用方给了正预算且真的装不下，**必须留下显式痕迹**，不能像以前那样静默丢掉最旧的：
    静默截断会让"早上说的事"凭空消失，而那正是记忆最该记住的部分。

    **带图的消息要标出来**：模型看不到图片本身，如果上下文里只有文字，
    它会对着"看这张"这三个字凭空作答。所以这里把附件数、有没有描述一起写进去。
    """
    if not hot:
        return ""
    chosen: list[str] = []
    used = 0
    dropped = 0
    blank_images = 0
    for m in reversed(hot):
        who = "用户" if m["role"] == "user" else "助手"
        line = f"[{m.get('created_at_local', '未知')}] {who}: {m['content']}"
        n_att = int(m.get("att_n") or 0)
        if n_att:
            desc = (m.get("att_desc") or "").strip()
            if desc:
                line += f"（附图 {n_att} 张，已描述：{desc}）"
            else:
                blank_images += 1
                line += f"（附图 {n_att} 张，未描述）"
        cost = estimate_tokens(line)
        if budget > 0 and used + cost > budget and chosen:
            dropped = len(hot) - len(chosen)
            break
        chosen.append(line)
        used += cost
    chosen.reverse()
    body = "\n".join(chosen)
    if dropped:
        head = (f"（这段对话较长，只带了最近 {len(chosen)} 条，更早的 {dropped} 条"
                f"没有注入；如果需要可以用检索查回来）\n")
    else:
        head = ""
    if blank_images:
        head += (f"（注意：这段对话里有 {blank_images} 条带图消息没有描述，"
                 f"你看不到图片内容；被问到图里是什么时直接说看不到，不要猜。）\n")
    return head + body


def render_hits(hits: list[dict], limit: int, per_hit: int) -> str:
    """冷记忆：检索命中，按相关度×时间衰减排好序，逐条截断。"""
    blocks = []
    for h in hits[:limit]:
        when = h.get("created_at_local", "未知")
        title = h.get("conversation_title") or "（无标题）"
        body = _clip(h.get("content", ""), per_hit)
        blocks.append(f"[{when}] 《{title}》\n{body}")
    return "\n\n".join(blocks)


def build_chat_messages(
    *,
    user_text: str,
    hot_text: str,
    hits_text: str,
    domain: str,
    clarify: str | None = None,
    gated: bool = False,
    now_line: str = "",
    persona: str = "",
) -> list[dict]:
    parts: list[str] = []
    if now_line:
        parts.append(f"【{now_line}】")
    if hot_text:
        parts.append("【最近对话（热记忆）】\n" + hot_text)
    if hits_text:
        label = "【证据（已通过审核，全部都要用到）】" if gated else "【检索到的历史片段（冷记忆）】"
        parts.append(label + "\n" + hits_text)
    if not parts:
        parts.append("【记忆片段】\n（无）")

    if clarify:
        parts.append("【待确认】\n" + clarify)

    context = "\n\n".join(parts)
    if clarify:
        system = SYSTEM_CLARIFY
    elif gated:
        system = SYSTEM_GATED
    else:
        system = SYSTEM_CHAT

    if persona.strip():
        system = PERSONA_TEMPLATE.format(persona=persona.strip()) + "\n\n" + system

    return [
        {"role": "system", "content": system},
        {"role": "system", "content": context},
        {"role": "user", "content": user_text},
    ]


def clarify_question(candidates: list[dict], user_text: str) -> str:
    """歧义时带候选反问。候选不是猜的答案，是让用户挑的锚点。"""
    if not candidates:
        return ("我不太确定「那件事」指的是哪一件。你可以补一个关键词，"
                "或者告诉我大概是什么时候吗？")
    lines = ["我不太确定你指的是哪一件，能确认一下吗？", ""]
    for i, c in enumerate(candidates[:3], 1):
        when = c.get("created_at_local", "未知时间")
        title = (c.get("conversation_title") or "").strip()
        snippet = (c.get("content") or "").replace("\n", " ")[:60]
        lines.append(f"{i}. {when}｜{title}｜{snippet}")
    lines.append("")
    lines.append("回复序号，或者直接补一句更具体的描述。")
    return "\n".join(lines)


def clarify_missing_object(domain: str, user_text: str) -> str:
    """歧义但**不需要回溯历史**时的回应：缺的是"对象"，不是"哪件事"。

    例如"帮我评估这篇论文"——它要的是一份文件，不是从历史对话里挑一场。
    这两类歧义必须分开，否则会拿一堆不相干的陈年对话当候选。
    """
    if domain == "work":
        return ("我需要知道具体处理对象。可以把文件（PDF / docx / 代码 / 表格）发给我，"
                "或者告诉我标题或链接，我再开始。")
    return ("这句话里我没找到具体的对象。能补一个名字、型号、地点，"
            "或者大概的时间范围吗？")


def insufficient_reply(missing: str, hint: str = "") -> str:
    """证据门的"不足"回应。

    注意它和 not_found_reply 的区别：这里**有命中**，只是不够。
    这两件事对用户是不同的信息，不能混成一句"没找到"。
    """
    lines = ["我查到了几条相关记录，但不足以回答这个问题。"]
    if missing:
        lines.append(f"缺的是：{missing}")
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def insufficient_ask(missing: str, hint: str = "",
                     keywords: list[str] | None = None) -> str:
    """证据门判"不足"时**先问细节**，而不是只陈述不足。

    用户 2026-11-06 定的偏好：指代太模糊时（"那家光学公司"），
    宁可反问时间/岗位/对象，也不要猜、也不要只丢一句"查到了但不够"。
    猜错的代价高于多问一句。
    """
    kw = "、".join(k for k in (keywords or []) if k)
    head = (f"我按「{kw}」查到几条相关记录，但不够回答这个问题。"
            if kw else "我查到几条相关记录，但不够回答这个问题。")
    lines = [head]
    if missing:
        lines.append(f"缺的是：{missing}")
    if hint:
        lines.append(hint)
    lines.append("能补一个细节吗——比如大概是什么时候，或者具体是哪家公司/哪个岗位？")
    return "\n".join(lines)


def not_found_reply(user_text: str, tried: list[str]) -> str:
    """检索为空的兜底。必须明确、可操作，且绝不编造。"""
    kw = "、".join(tried) if tried else "（没有提取到关键词）"
    return (f"我在本地记录里没有找到你说的那个。\n"
            f"我试过的关键词：{kw}\n"
            f"你可以补充一个具体的名字、型号或地点，或者告诉我大概的时间范围，我再查一次。")
