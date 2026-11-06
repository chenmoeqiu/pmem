"""模型提供方：薄接口 + 裸 HTTP 实现 + 离线降级实现。

为什么是一层薄接口而不是直接写死调用：
- 开源版的默认路径必须是裸 API（别人没有 DSH）
- 换供应商只改 base_url / model 名
- 没有 key 时能降级成"规则 + 模板"，链路照样跑通、照样可测

第三方依赖：零。只用 urllib。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from .config import Config

# ---------------------------------------------------------------- 接口


class Provider(Protocol):
    name: str

    def classify(self, user_text: str, recent: list[dict],
                 vocab: list[str] | None = None) -> tuple[dict, dict]:
        """返回 (决策, 用量)。决策字段见 route.py。"""
        ...

    def assess(self, question: str, hits: list[dict]) -> tuple[dict, dict]:
        """证据门：返回 (判定, 用量)。判定字段见 gate.py。"""
        ...

    def synthesize(self, messages: list[dict]) -> tuple[str, dict]:
        """返回 (回复文本, 用量)。"""
        ...

    def describe_image(self, data: bytes, mime: str,
                       hint: str = "") -> tuple[str, dict]:
        """给一张图写一句话描述。离线实现返回空串（等于没做，不是编一个）。"""
        ...


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- 工具


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def extract_json(text: str) -> dict | None:
    """从模型输出里尽力抠出 JSON。容忍代码围栏和前后废话。"""
    if not text:
        return None
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def load_api_key(cfg: Config) -> str | None:
    """密钥来源：环境变量优先，其次项目根目录的 secrets.toml（已 gitignore）。"""
    env_name = cfg.get("llm", "api_key_env", default="DEEPSEEK_API_KEY")
    val = os.environ.get(env_name) if env_name else None
    if val:
        return val.strip()

    secrets = cfg.root / "secrets.toml"
    if secrets.exists():
        try:
            import tomllib
            with secrets.open("rb") as f:
                data = tomllib.load(f)
            for key in ("api_key", "deepseek_api_key", env_name.lower() if env_name else ""):
                if key and isinstance(data.get("llm", {}).get(key), str):
                    return data["llm"][key].strip()
                if key and isinstance(data.get(key), str):
                    return data[key].strip()
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- HTTP 实现


class HttpProvider:
    """OpenAI 兼容的 /chat/completions。DeepSeek、多数国内厂商都符合。"""

    name = "api"

    def __init__(self, cfg: Config, api_key: str):
        self.cfg = cfg
        self.api_key = api_key
        self.base_url = str(cfg.get("llm", "base_url")).rstrip("/")
        self.model_router = str(cfg.get("llm", "model_router"))
        self.model_synth = str(cfg.get("llm", "model_synth"))
        self.timeout = int(cfg.get("llm", "timeout_seconds", default=60))
        self.retries = int(cfg.get("llm", "max_retries", default=1))
        self.json_mode = bool(cfg.get("llm", "json_mode", default=True))

    # -- 底层

    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                last = LLMError(f"HTTP {e.code}: {detail}")
                if e.code in (400, 401, 403, 404):
                    break  # 重试无意义
                time.sleep(1.2 * (attempt + 1))
            except Exception as e:  # 超时、连接失败
                last = LLMError(f"{type(e).__name__}: {e}")
                time.sleep(1.2 * (attempt + 1))
        raise last or LLMError("unknown error")

    def _chat(self, model: str, messages: list[dict], *, temperature: float,
              json_mode: bool = False, max_tokens: int = 900) -> tuple[str, dict]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode and self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(payload)
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"响应结构异常: {data}") from e
        usage = data.get("usage") or {}
        return content, {
            "model": model,
            "tokens_in": usage.get("prompt_tokens"),
            "tokens_out": usage.get("completion_tokens"),
        }

    # -- 业务

    def classify(self, user_text: str, recent: list[dict],
                 vocab: list[str] | None = None) -> tuple[dict, dict]:
        from .prompt import build_router_messages, current_time_line
        now_line = current_time_line(float(self.cfg.get("runtime", "utc_offset_hours", default=8)))
        msgs = build_router_messages(user_text, recent, now_line=now_line, vocab=vocab)
        raw, usage = self._chat(
            self.model_router, msgs,
            temperature=float(self.cfg.get("llm", "temperature_router", default=0)),
            json_mode=True, max_tokens=300,
        )
        data = extract_json(raw)
        if data is None:
            raise LLMError(f"路由输出不是 JSON: {raw[:200]}")
        return data, usage

    def tag_batch(self, messages: list[dict], *, max_tokens: int = 900) -> tuple[str, dict]:
        """打标/归类：批量、强制 JSON、温度 0（同一批结果要可复现）。"""
        return self._chat(
            self.model_router, messages,
            temperature=0.0, json_mode=True, max_tokens=max_tokens,
        )

    def describe_image(self, data: bytes, mime: str,
                       hint: str = "") -> tuple[str, dict]:
        """给一张图写一句话描述（视觉模型）。

        走 OpenAI 兼容的 image_url + base64。模型不支持图片时这里会抛错，
        由调用方计入 failed——**不要退回"猜一个描述"**，那会污染记忆。
        """
        b64 = base64.b64encode(data).decode("ascii")
        msgs = [
            {"role": "system",
             "content": "你只做客观描述，不评价、不推测用途。一句话，中文。"},
            {"role": "user", "content": [
                {"type": "text", "text": hint or "描述这张图里有什么。"},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime or 'image/jpeg'};base64,{b64}"}},
            ]},
        ]
        model = str(self.cfg.get("llm", "model_vision", default=self.model_router))
        raw, usage = self._chat(model, msgs, temperature=0.0, max_tokens=200)
        return (raw or "").strip(), usage

    def reconsider(self, user_text: str, recent: list[dict]) -> tuple[dict, dict]:
        """第三档复核：只在"判了不回溯但句子涉及我的处境"时调用。"""
        from .prompt import build_second_opinion_messages, current_time_line
        now_line = current_time_line(float(self.cfg.get("runtime", "utc_offset_hours", default=8)))
        msgs = build_second_opinion_messages(user_text, recent, now_line=now_line)
        raw, usage = self._chat(
            self.model_router, msgs,
            temperature=0.0, json_mode=True, max_tokens=200,
        )
        data = extract_json(raw)
        if data is None:
            raise LLMError(f"复核输出不是 JSON: {raw[:200]}")
        return data, usage

    def assess(self, question: str, hits: list[dict]) -> tuple[dict, dict]:
        from .gate import build_messages
        from .prompt import current_time_line
        now_line = current_time_line(float(self.cfg.get("runtime", "utc_offset_hours", default=8)))
        msgs = build_messages(question, hits, now_line=now_line)
        raw, usage = self._chat(
            self.model_router, msgs,
            temperature=0.0, json_mode=True, max_tokens=260,
        )
        data = extract_json(raw)
        if data is None:
            raise LLMError(f"证据门输出不是 JSON: {raw[:200]}")
        return data, usage

    def chat_with_tools(
        self, messages: list[dict], tools: list[dict], *,
        temperature: float = 0.0, max_tokens: int = 500,
    ) -> tuple[dict, dict]:
        """原生工具调用（OpenAI 兼容格式）。给对照实验的 agent 组用。"""
        payload = {
            "model": self.model_router,
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = self._post(payload)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"响应结构异常: {data}") from e
        usage = data.get("usage") or {}
        return msg, {
            "model": self.model_router,
            "tokens_in": usage.get("prompt_tokens"),
            "tokens_out": usage.get("completion_tokens"),
        }

    def synthesize(self, messages: list[dict]) -> tuple[str, dict]:
        return self._chat(
            self.model_synth, messages,
            temperature=float(self.cfg.get("llm", "temperature_synth", default=0.7)),
            json_mode=False, max_tokens=1200,
        )


# ---------------------------------------------------------------- 离线实现


_RECALL_WORDS = ("上次", "上一次", "之前", "以前", "我说过", "我跟你说", "记得",
                 "还记得", "那次", "那件事", "刚才说", "继续那个", "前面提到",
                 "前几天", "上周", "上个月", "去年", "历史", "回顾")
_WORK_WORDS = ("论文", "代码", "报错", "调试", "调研", "评估", "实现", "架构",
               "sql", "python", "api", "方案", "写一个", "改一下", "优化",
               "分析", "计算", "推导")
_VAGUE_WORDS = ("那件事", "那个事", "就那个", "那个东西", "那玩意儿", "这件事")


class OfflineProvider:
    """没有 API key 时的降级实现：路由用规则，回复只给一句说明，不生成。

    注意：这里**绝不回显上下文**。回显会让上一轮的整段上下文进入下一轮热窗，
    形成自我污染和 token 膨胀。
    """

    name = "offline"

    @staticmethod
    def _keywords(text: str) -> list[str]:
        import re
        # 优先取实体：型号、股票代码、英文缩写
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-_.]{2,}", text)
        if tokens:
            return tokens[:3]
        cleaned = text
        for w in _RECALL_WORDS:
            cleaned = cleaned.replace(w, " ")
        segs = re.findall(r"[\u4e00-\u9fff]{2,}", cleaned)
        return sorted(set(segs), key=len, reverse=True)[:2]

    def classify(self, user_text: str, recent: list[dict],
                 vocab: list[str] | None = None) -> tuple[dict, dict]:
        from .route import _literal_tags
        text = user_text.lower()
        need_recall = any(w in user_text for w in _RECALL_WORDS)
        domain = "work" if any(w in text for w in _WORK_WORDS) else "casual"
        keywords = self._keywords(user_text) if need_recall else []
        vague = any(w in user_text for w in _VAGUE_WORDS)
        return ({
            "domain": domain,
            "need_recall": need_recall,
            "keywords": keywords,
            "ambiguous": vague and not keywords,
            "tags": _literal_tags(user_text, vocab),
            "reason": "离线规则判定",
        }, {"model": "offline", "tokens_in": 0, "tokens_out": 0})

    def tag_batch(self, messages: list[dict], *, max_tokens: int = 900) -> tuple[str, dict]:
        """离线不打标：返回空结果，让调用方跳过这一批。"""
        return "{}", {"model": "offline", "tokens_in": 0, "tokens_out": 0}

    def describe_image(self, data: bytes, mime: str,
                       hint: str = "") -> tuple[str, dict]:
        """离线不描述：返回空串。**不编造描述**——空着比假的强。"""
        return "", {"model": "offline", "tokens_in": 0, "tokens_out": 0}

    def reconsider(self, user_text: str, recent: list[dict]) -> tuple[dict, dict]:
        """离线版没有判断力，一律不改判——避免用规则制造新的误判。"""
        return ({"needs_history": False, "keywords": [], "reason": "离线模式不复核"},
                {"model": "offline", "tokens_in": 0, "tokens_out": 0})

    def assess(self, question: str, hits: list[dict]) -> tuple[dict, dict]:
        """离线版的证据门：只用规则——有命中就算够，但要全部用上。"""
        used = list(range(1, len(hits) + 1))
        if not hits:
            return ({"sufficient": False, "used": [], "missing": "没有任何命中",
                     "next": "stop"}, {"model": "offline", "tokens_in": 0, "tokens_out": 0})
        return ({"sufficient": True, "used": used, "missing": "", "next": "answer"},
                {"model": "offline", "tokens_in": 0, "tokens_out": 0})

    def synthesize(self, messages: list[dict]) -> tuple[str, dict]:
        return (
            "（离线模式：未配置 API key，本条不做生成。）\n"
            "路由与检索都是真实执行的，可以看下方提示里的命中数；"
            "要拿到真正的回答，请在 secrets.toml 里填 api_key，或用 /recall 直接看原文。"
        ), {"model": "offline", "tokens_in": 0, "tokens_out": 0}


# ---------------------------------------------------------------- 工厂


def build(cfg: Config, force_offline: bool = False) -> Provider:
    mode = "offline" if force_offline else str(cfg.get("llm", "provider", default="auto")).lower()

    if mode == "offline":
        return OfflineProvider()

    key = load_api_key(cfg)
    if not key:
        if mode == "api":
            raise SystemExit(
                "配置了 provider=api 但找不到 API key。\n"
                "  方式一：设置环境变量 DEEPSEEK_API_KEY\n"
                "  方式二：在项目根目录建 secrets.toml，内容：\n"
                '    [llm]\n    api_key = "sk-..."\n'
                "  （secrets.toml 已在 .gitignore 中）"
            )
        return OfflineProvider()

    return HttpProvider(cfg, key)
