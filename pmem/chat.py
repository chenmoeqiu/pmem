"""对话链路：路由 -> 检索/热窗 -> 生成 -> 落库。

三条支路，行为完全不同：
- 闲聊：注入热窗（最近 24h 整段），不带冷记忆
- 工作：**不注入热窗**（上下文隔离），需要时只带检索命中
- 歧义：不回答，先带候选反问
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from . import clock, gate, prompt, route, search, store
from .config import Config

KIND_CHAT = "chat"
KIND_CLARIFY = "clarify"
KIND_NOT_FOUND = "not_found"


@dataclass
class TurnResult:
    reply: str
    kind: str
    decision: route.Decision
    hits: list[dict[str, Any]] = field(default_factory=list)
    conversation_id: int | None = None
    gate: Any = None
    retrieved: int = 0


def _today_local(off: float) -> str:
    stamp, _ = clock.local_stamp(None, off)
    return stamp.split("-")[0]


class ChatSession:
    def __init__(self, con: sqlite3.Connection, cfg: Config, provider):
        self.con = con
        self.cfg = cfg
        self.provider = provider
        self.off = float(cfg.get("runtime", "utc_offset_hours", default=8))
        self.conversation_id: int | None = None
        self._pending: dict[str, Any] | None = None
        # 上一轮"顺手整理"的结果（没触发时是 skipped），给调用方看用
        self.last_daily: dict[str, Any] | None = None

    # ------------------------------------------------------------ 基础

    def ensure_conversation(self) -> int:
        if self.conversation_id is not None:
            return self.conversation_id
        cid = store.upsert_conversation(
            self.con,
            source="live",
            source_id=f"live-{_today_local(self.off)}",
            title=f"实时对话 {_today_local(self.off)[:4]}-{_today_local(self.off)[4:6]}-{_today_local(self.off)[6:]}",
            started_at=clock.now_utc_iso(),
            time_precision="message",
        )
        self.conversation_id = cid
        return cid

    def _recent(self, limit: int = 6) -> list[dict[str, Any]]:
        cid = self.ensure_conversation()
        rows = self.con.execute(
            "SELECT role, content FROM messages "
            "WHERE conversation_id = ? AND state = 'active' ORDER BY seq DESC LIMIT ?",
            (cid, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def _add(self, role: str, content: str) -> int:
        cid = self.ensure_conversation()
        mid = store.add_message(
            self.con, conversation_id=cid, role=role, origin=role,
            content=content, created_at=clock.now_utc_iso(),
            time_precision="message", source="live",
        )
        # 一轮结束（助手那条写完）顺手整理"睡过去的那一段"。
        # 队列空时只是一次 SQL；失败不能影响对话，所以整块兜住。
        if role == "assistant":
            try:
                from . import daily as _daily
                self.last_daily = _daily.maybe_auto(self.con, self.cfg, self.provider)
            except Exception as e:  # noqa: BLE001
                self.last_daily = {"ok": False, "error": type(e).__name__}
        return mid

    # ------------------------------------------------------------ 候选

    def _recent_conversations(self, days: int = 21, limit: int = 3) -> list[dict[str, Any]]:
        """为"那件事"这类指代，捞出最近几场对话当候选锚点。"""
        since = clock.window_start_iso(days * 24)
        cid = self.ensure_conversation()
        rows = self.con.execute(
            """
            SELECT m.conversation_id, c.title, MAX(m.created_at) AS last_at
              FROM messages m JOIN conversations c ON c.id = m.conversation_id
             WHERE m.state = 'active' AND m.role = 'user'
               AND m.created_at >= ? AND m.conversation_id <> ?
             GROUP BY m.conversation_id
             ORDER BY last_at DESC LIMIT ?
            """,
            (since, cid, limit),
        ).fetchall()
        out = []
        for r in rows:
            last = self.con.execute(
                "SELECT content, created_at FROM messages WHERE conversation_id = ? "
                "AND role = 'user' AND state = 'active' ORDER BY seq DESC LIMIT 1",
                (r["conversation_id"],),
            ).fetchone()
            if not last:
                continue
            out.append({
                "conversation_id": r["conversation_id"],
                "conversation_title": r["title"],
                "content": last["content"],
                "created_at_local": clock.local_str(last["created_at"], self.off),
            })
        return out

    def _conversation_context(self, conversation_id: int, limit: int = 6) -> list[dict[str, Any]]:
        rows = self.con.execute(
            """SELECT m.content, m.created_at, c.title AS conversation_title
                 FROM messages m JOIN conversations c ON c.id = m.conversation_id
                WHERE m.conversation_id = ? AND m.state = 'active'
                ORDER BY m.seq DESC LIMIT ?""",
            (conversation_id, limit),
        ).fetchall()
        return [{
            "content": r["content"],
            "created_at": r["created_at"],
            "created_at_local": clock.local_str(r["created_at"], self.off),
            "conversation_title": r["conversation_title"],
            "score": 0.0,
        } for r in reversed(rows)]

    # ------------------------------------------------------------ 主流程

    def _vocab_names(self) -> list[str]:
        """已批准标签名，给路由选标签用。一次会话内不变，缓存起来。"""
        vocab = getattr(self, "_vocab", None)
        if vocab is None:
            from . import tagging
            vocab = [t["name"] for t in tagging.vocabulary(self.con)]
            self._vocab = vocab
        return vocab

    def respond(self, user_text: str, *, force_recall: bool = False) -> TurnResult:
        user_text = (user_text or "").strip()
        if not user_text:
            raise ValueError("空输入")

        # 1. 热记忆边界 = 当前"清醒段"的起点。注入与检索排除必须**同源**，
        #    否则会出现"既没注入又查不到"的空洞（滑出去的内容必须可检索）。
        since = search.segment_start_iso(self.con, self.cfg)
        if since:
            # 睡了一觉：上一段转冷，只入队；真正的切段+打标留到闲时（谷价时段）
            from . import tagging
            tagging.mark_cold_before(self.con, since)
        else:
            since = clock.window_start_iso(
                int(self.cfg.get("chat", "hot_hours", default=24)))
        hot = search.hot_window(
            self.con, self.cfg, hours=int(self.cfg.get("chat", "hot_hours", default=24)),
            mention=user_text, since=since,
        )

        # 2. 澄清的后续：用户回了序号
        pending = self._pending
        self._pending = None
        if pending and user_text.strip() in {str(i) for i in range(1, 5)}:
            idx = int(user_text.strip()) - 1
            cands = pending["candidates"]
            if 0 <= idx < len(cands):
                return self._answer_candidate(cands[idx], pending["question"])

        # 3. 路由（带上已批准词表，让模型选标签：标签是检索主路径）
        d = route.decide(self.provider, user_text, self._recent(),
                         vocab=self._vocab_names())
        if force_recall:
            d.need_recall = True
            d.reason = (d.reason + "｜手动强制检索").strip("｜")
            if not d.keywords:
                d.keywords = [user_text[:20]]

        uid = self._add("user", user_text)

        # 4. 检索：标签为主路径 + 全文兜底；只排除热记忆那一段（不排除整场会话）
        hits: list[dict[str, Any]] = []
        if d.need_recall:
            limit = int(self.cfg.get("chat", "hits_limit", default=8))
            if d.keywords:
                hits = search.search_multi(
                    self.con, self.cfg, d.keywords, limit=limit,
                    mention=user_text, exclude_since=since,
                )
            tag_hits: list[dict[str, Any]] = []
            for tg in (d.tags or [])[:4]:
                tag_hits += search.search_by_tag(
                    self.con, self.cfg, tg, limit=max(3, limit // 2),
                    mention=user_text, exclude_since=since)
            if tag_hits:
                hits = search.merge_hits(hits, tag_hits, limit)
            hits = [h for h in hits if h["id"] != uid]
            # 一个关键词如果一条都没命中，说明它不是实体（例如"送朋友礼物"），
            # 从词表里剔掉，免得污染后续的证据门和日志
            matched = {h.get("matched_keyword") for h in hits}
            kept = [k for k in d.keywords if k in matched]
            if kept and len(kept) != len(d.keywords):
                d.keywords = kept

        # 5. 歧义分支：先问，不答。但要区分两类歧义——
        #    要回溯历史的（"那件事"）用候选反问；
        #    不要回溯的（"这篇论文"没说是哪篇）只问缺的对象。
        if d.ambiguous:
            if d.need_recall:
                cands = self._recent_conversations()
                question = prompt.clarify_question(cands, user_text)
                self._pending = {"candidates": cands, "question": user_text}
                hits_for_log = len(cands)
            else:
                question = prompt.clarify_missing_object(d.domain, user_text)
                cands = []
                hits_for_log = 0
            self._add("assistant", question)
            route.log_decision(
                self.con, conversation_id=self.conversation_id,
                user_text=user_text, d=d, hits=hits_for_log,
            )
            return TurnResult(question, KIND_CLARIFY, d, cands, self.conversation_id)

        # 6. 要回溯但没命中：明说，不编
        if d.need_recall and not hits:
            reply = prompt.not_found_reply(user_text, d.keywords)
            self._add("assistant", reply)
            route.log_decision(
                self.con, conversation_id=self.conversation_id,
                user_text=user_text, d=d, hits=0,
                gate_sufficient=False, gate_used=[], gate_missing="没有任何命中",
            )
            return TurnResult(reply, KIND_NOT_FOUND, d, [], self.conversation_id)

        # 6.5 证据门：命中不等于够用。这一步决定"能不能答"，而不是"怎么答"。
        verdict = None
        gated_hits = hits
        gate_enabled = bool(self.cfg.get("chat", "gate_enabled", default=True))
        if gate_enabled and hits:
            try:
                vraw, vu = self.provider.assess(user_text, hits)
                verdict = gate.validate(vraw, len(hits))
                d.tokens_in = (d.tokens_in or 0) + (vu.get("tokens_in") or 0)
                d.tokens_out = (d.tokens_out or 0) + (vu.get("tokens_out") or 0)
            except Exception as e:
                verdict = None  # 门本身失败不阻断对话，但会在日志里体现为 None

            if verdict is not None and not verdict.sufficient:
                # 用户偏好（2026-11-06）：证据不足时**先反问细节**，而不是只丢一句"不够"。
                # 指代太模糊（"那家光学公司"）时尤其如此：猜错的代价高于多问一句。
                reply = prompt.insufficient_ask(verdict.missing, verdict.hint(),
                                                d.keywords)
                self._add("assistant", reply)
                route.log_decision(
                    self.con, conversation_id=self.conversation_id,
                    user_text=user_text, d=d, hits=len(hits),
                    gate_sufficient=False, gate_used=verdict.used,
                    gate_missing=verdict.missing,
                )
                return TurnResult(reply, KIND_CLARIFY, d,
                                  [hits[i - 1] for i in verdict.used] if verdict.used else [],
                                  self.conversation_id, gate=verdict, retrieved=len(hits))

            if verdict is not None and verdict.used:
                # 只把"门的判定里点名要用"的那几条交给生成器，
                # 这样就不存在"召回 5 条、只引用 2 条"
                gated_hits = [hits[i - 1] for i in verdict.used]

        # 7. 正常生成
        hot_text = "" if d.domain == "work" else prompt.render_hot(
            hot, int(self.cfg.get("chat", "hot_max_tokens", default=0))
        )
        hits_text = prompt.render_hits(
            gated_hits,
            int(self.cfg.get("chat", "hits_limit", default=8)),
            int(self.cfg.get("chat", "hit_max_tokens", default=300)),
        )
        # 图片描述作为"相关图片"块附在证据后面：不占证据编号，但模型看得到
        if d.need_recall and d.keywords:
            from . import images as _images
            img_rows: list[dict[str, Any]] = []
            for kw in d.keywords[:3]:
                img_rows += _images.search(self.con, kw, limit=2)
            img_text = _images.render(img_rows)
            if img_text:
                hits_text = f"{hits_text}\n\n【相关图片】\n{img_text}".strip()
        messages = prompt.build_chat_messages(
            user_text=user_text, hot_text=hot_text, hits_text=hits_text,
            domain=d.domain, gated=verdict is not None and verdict.sufficient,
            now_line=prompt.current_time_line(self.off),
            persona=prompt.load_persona(self.cfg),
        )
        try:
            t_synth = time.time()
            reply, usage = self.provider.synthesize(messages)
            # 路由、证据门、生成是三次独立调用，成本必须合并统计
            d.tokens_in = (d.tokens_in or 0) + (usage.get("tokens_in") or 0)
            d.tokens_out = (d.tokens_out or 0) + (usage.get("tokens_out") or 0)
            d.latency_ms += int((time.time() - t_synth) * 1000)
        except Exception as e:
            reply = f"（生成失败：{type(e).__name__}）我这边模型暂时不可用，但检索本身是正常的。"
            usage = {}

        self._add("assistant", reply)
        route.log_decision(
            self.con, conversation_id=self.conversation_id,
            user_text=user_text, d=d, hits=len(gated_hits),
            gate_sufficient=None if verdict is None else verdict.sufficient,
            gate_used=None if verdict is None else verdict.used,
            gate_missing=None if verdict is None else verdict.missing,
        )
        return TurnResult(reply, KIND_CHAT, d, gated_hits, self.conversation_id,
                          gate=verdict, retrieved=len(hits))

    def _answer_candidate(self, cand: dict[str, Any], original_question: str) -> TurnResult:
        hits = self._conversation_context(int(cand["conversation_id"]))
        hits_text = prompt.render_hits(
            hits, len(hits), int(self.cfg.get("chat", "hit_max_tokens", default=300))
        )
        messages = prompt.build_chat_messages(
            user_text=original_question, hot_text="", hits_text=hits_text, domain="casual",
            now_line=prompt.current_time_line(self.off),
            persona=prompt.load_persona(self.cfg),
        )
        try:
            reply, _ = self.provider.synthesize(messages)
        except Exception as e:
            reply = f"（生成失败：{type(e).__name__}）"
        self._add("assistant", reply)
        d = route.Decision(domain="casual", need_recall=True, keywords=[],
                           source="clarify", reason="按候选确认后回答")
        route.log_decision(
            self.con, conversation_id=self.conversation_id,
            user_text=f"[候选确认] {cand.get('conversation_title')}",
            d=d, hits=len(hits),
        )
        return TurnResult(reply, KIND_CHAT, d, hits, self.conversation_id)

    # ------------------------------------------------------------ 手动

    def recall(self, query: str) -> TurnResult:
        """手动强制检索：绕过路由，直接给原文。"""
        limit = int(self.cfg.get("chat", "hits_limit", default=8))
        since = search.segment_start_iso(self.con, self.cfg) or clock.window_start_iso(
            int(self.cfg.get("chat", "hot_hours", default=24)))
        hits = search.search_multi(
            self.con, self.cfg, [query], limit=limit,
            exclude_conversation=self.conversation_id,
            exclude_since=since,
            mention=query,
        )

        d = route.Decision(domain="casual", need_recall=True, keywords=[query],
                           source="manual", reason="用户手动 /recall")
        if not hits:
            reply = prompt.not_found_reply(query, [query])
            return TurnResult(reply, KIND_NOT_FOUND, d, [], self.conversation_id)
        return TurnResult(prompt.render_hits(hits, len(hits), 400), KIND_CHAT, d,
                          hits, self.conversation_id)
