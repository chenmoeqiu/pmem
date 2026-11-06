"""中文检索的文本处理。

为什么需要这个模块：
SQLite FTS5 的 unicode61 分词器会把一串连续汉字当成"一个词"，
所以 "今天天气怎么样" 是单个 token，搜 "天气" 命中 0 条。
trigram 分词器也不行：它要求检索词至少 3 个字符，而中文最常用的
词是两个字（键盘/天气/老板）。

本模块的做法是"预分词索引列"：入库时在每个汉字之间插空格，
使每个汉字成为一个 token；检索时用 phrase 查询（"天 气"），
连续 token 序列 == 原文的连续子串，于是得到精确子串匹配。

关键约定：入库与检索必须走同一个 tokenize()。这是本模块存在的意义，
也是代码库其他地方唯一需要知道的事。
"""

from __future__ import annotations

import re

# 汉字 + 日文假名 + 韩文音节：凡是不能靠空格/标点自然切分的文字，都按单字切
_CJK_RANGES = (
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0x4E00, 0x9FFF),   # CJK 基本区
    (0xF900, 0xFAFF),   # CJK 兼容
    (0x3040, 0x30FF),   # 平假名 / 片假名
    (0xAC00, 0xD7AF),   # 韩文音节
    (0x20000, 0x2FA1F), # CJK 扩展 B+
)

_WS_RE = re.compile(r"\s+")
_TERM_SPLIT_RE = re.compile(r"[\s,，、;；/|]+")


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def tokenize(text: str) -> str:
    """把文本变成 FTS5 友好的形式：汉字逐字空格分隔，西文保持原样。"""
    if not text:
        return ""
    out = []
    for ch in text:
        if is_cjk(ch):
            out.append(" ")
            out.append(ch)
            out.append(" ")
        else:
            out.append(ch)
    return _WS_RE.sub(" ", "".join(out)).strip()


def _quote(term: str) -> str:
    """把一个词包成 FTS5 phrase，中文会自然变成 "天 气" 形式。"""
    t = tokenize(term)
    if not t:
        return ""
    t = t.replace('"', '""')
    return f'"{t}"'


def build_match(query: str) -> str:
    """把用户的自然检索串变成 FTS5 MATCH 表达式（词组用 AND 连接）。"""
    if not query:
        return ""
    terms = [t for t in _TERM_SPLIT_RE.split(query.strip()) if t]
    if not terms:
        terms = [query.strip()]
    parts = [_quote(t) for t in terms]
    return " AND ".join(p for p in parts if p)


def normalize_for_key(text: str) -> str:
    """用于去重/比较的归一化：压空白、去零宽字符。"""
    t = text.replace("\u200b", "").replace("\ufeff", "")
    return _WS_RE.sub(" ", t).strip()


def make_snippet(content: str, query: str, width: int = 70) -> str:
    """在原文上做片段抽取。

    不用 FTS5 的 snippet()：索引列是插了空格的，snippet 会把
    "冰静轴" 显示成 "冰 静 轴"。
    """
    if not content:
        return ""
    flat = content.replace("\n", " ")
    if not query:
        return flat[:width] + ("…" if len(flat) > width else "")

    terms = [t for t in _TERM_SPLIT_RE.split(query.strip()) if t] or [query.strip()]
    lowered = flat.lower()
    pos = -1
    hit = ""
    for t in terms:
        p = lowered.find(t.lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos, hit = p, t
    if pos < 0:
        return flat[:width] + ("…" if len(flat) > width else "")

    start = max(0, pos - width // 2)
    end = min(len(flat), pos + len(hit) + width // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{flat[start:end]}{suffix}"
