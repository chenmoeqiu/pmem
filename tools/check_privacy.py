"""隐私扫描：防止"又一次把真实数据带进仓库"。

为什么需要它：脱敏靠人眼一定会漏。真正管用的是**机械检查**——
每次 `run_all` 都扫一遍，命中就红。词表本身不能进仓库（它也是敏感信息），
所以放在 `privacy_terms.txt`（已 gitignore）；没有这个文件时，只用内置的
结构性规则（邮箱、密钥、本机绝对路径、长数字串）。

用法：
    python tools/check_privacy.py
    python tools/check_privacy.py --list     # 只看扫了哪些文件

退出码：0 干净；1 命中。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if sys.version_info < (3, 11):
    print(f"需要 Python 3.11+（当前是 {sys.version.split()[0]}）：tomllib 在 3.11 才有。")
    print(f"换一个解释器再跑，例如：  py -3.12 {Path(__file__).name}")
    raise SystemExit(2)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SKIP_DIRS = {".git", "data", "__pycache__", ".venv", "venv", "node_modules", "_backups"}
SKIP_FILES = {"privacy_terms.txt", "persona.md", "secrets.toml"}
TEXT_SUFFIX = {".py", ".md", ".toml", ".cmd", ".ps1", ".txt", ".json",
               ".yml", ".yaml", ".cfg", ".ini", ".example"}

# 结构性规则：不依赖任何自定义词表，换谁都能跑
PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "邮箱"),
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "疑似 API key"),
    (re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\s\"']+"), "本机绝对路径"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "疑似手机号"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "疑似身份证号"),
]


def _files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name in SKIP_FILES:
            continue
        if p.suffix.lower() in TEXT_SUFFIX or p.name.startswith("LICENSE"):
            out.append(p)
    return sorted(out)


def _terms() -> list[str]:
    f = ROOT / "privacy_terms.txt"
    if not f.is_file():
        return []
    return [t.strip() for t in f.read_text(encoding="utf-8-sig").splitlines()
            if t.strip() and not t.strip().startswith("#")]


def main(argv: list[str]) -> int:
    files = _files()
    terms = _terms()
    if "--list" in argv:
        for p in files:
            print(p.relative_to(ROOT))
        print(f"\n共 {len(files)} 个文件；自定义词表 {len(terms)} 条")
        return 0

    hits: list[str] = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = p.relative_to(ROOT)
        for i, line in enumerate(text.splitlines(), 1):
            for pat, label in PATTERNS:
                if pat.search(line):
                    hits.append(f"{rel}:{i}  [{label}]  {line.strip()[:90]}")
            for t in terms:
                if t in line:
                    hits.append(f"{rel}:{i}  [词表:{t}]  {line.strip()[:90]}")

    if hits:
        print(f"[FAIL] 隐私扫描：{len(files)} 个文件，命中 {len(hits)} 处")
        for h in hits[:30]:
            print("   " + h)
        if len(hits) > 30:
            print(f"   …… 还有 {len(hits) - 30} 处")
        print("\n处理方式：换成中性写法，或把该词加进 privacy_terms.txt（它本身不进仓库）。")
        return 1

    print(f"[PASS] 隐私扫描：{len(files)} 个文件，命中 0 处"
          + (f"（自定义词表 {len(terms)} 条）" if terms else "（无自定义词表，只用结构规则）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
