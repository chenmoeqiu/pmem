"""配置加载：标准库 tomllib（Python 3.11+），零第三方依赖。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "paths": {
        "db": "data/memory.db",
        "media": "data/media",
        "export": "data/export",
        "profile": "data/profile",
    },
    "runtime": {
        # 本地时区偏移（小时）。不依赖系统 tzdata，Windows 上更稳。
        "utc_offset_hours": 8,
    },
    "import": {
        "skip_video": True,
        "max_attachment_mb": 20,
        "timezone_offset_minutes": 480,
    },
    "search": {
        "recall": 200,          # 先宽召回这么多条，再在 Python 侧重排
        "default_limit": 10,
        "half_life_days": 180,  # 时间衰减半衰期
    },
    "llm": {
        "provider": "auto",                      # auto | api | offline
        "base_url": "https://api.deepseek.com",
        "model_router": "deepseek-chat",
        "model_synth": "deepseek-chat",
        "model_vision": "deepseek-chat",   # 图片描述用；换支持视觉的模型时改这里
        "api_key_env": "DEEPSEEK_API_KEY",
        "timeout_seconds": 60,
        "max_retries": 1,
        "json_mode": True,
        "temperature_router": 0.0,
        "temperature_synth": 0.7,
    },
    "chat": {
        "hot_hours": 24,        # 找不到睡眠边界时的回退窗口
        "hot_max_tokens": 0,    # 0 = 不截断（热记忆 = 整段注入）；正数才限制
        "sleep_gap_hours": 6,   # 间隔≥这么久才可能算"睡了一觉"
        "sleep_window": "01:00-08:00",   # 睡眠窗口：间隔必须与它重叠才判为睡眠
        "sleep_min_overlap_hours": 2,    # 与窗口的最小重叠小时数（防边缘误判）
        "hits_limit": 8,        # 召回上限（证据门再从里面挑）
        "hit_max_tokens": 300,  # 单条命中最多占多少
        "gate_enabled": True,   # 证据门：命中不等于够用，先判再答
        # 人设/语气：默认读项目根下的 persona.md（gitignore）。
        # 想临时试别的语气，直接在 config.toml 里写 persona = "一句描述" 即可覆盖文件。
        "persona_file": "persona.md",
        "persona": "",
    },
    "cold": {
        # 转冷之后自动整理（切段 + 打标）。true 时，一轮对话结束顺手跑一点点。
        "auto": True,
        "limit": 2,             # 每次最多整理几场（其余的留给下一轮）
        "interval_hours": 6,    # 两次整理之间至少间隔多久
    },
}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any], root: Path):
        self.data = data
        self.root = root

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def path(self, *keys: str) -> Path:
        raw = self.get(*keys)
        if raw is None:
            raise KeyError(f"配置缺失: {'.'.join(keys)}")
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p)


def load(config_path: Path | None = None) -> Config:
    root = project_root()
    cfg_file = config_path or (root / "config.toml")
    data = DEFAULTS
    if cfg_file.exists():
        with cfg_file.open("rb") as f:
            data = _merge(DEFAULTS, tomllib.load(f))
    return Config(data, root)
