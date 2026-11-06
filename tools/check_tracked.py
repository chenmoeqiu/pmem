"""检查「被 git 跟踪的文件」里有没有不该进仓库的东西。

`.gitignore` 只防手滑，不防 `git add -f`，也不防有人把密钥写进 `config.toml` 再提交。
这个脚本查的是**索引里的文件清单**——也就是"一旦 push 就会公开"的那一份：

- 密钥/凭据文件：`secrets.toml`、`.env`、`*.key`、`*.pem`
- 个人数据：`data/` 下任何文件、`persona.md`、`privacy_terms.txt`
- 数据库：`*.db`

用法：
    python tools/check_tracked.py

退出码：0 干净（或不在 git 仓库里）；1 发现不该跟踪的文件。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 和 pmem/db.py 里同一套找法：Windows 上装了 Git 也常常不在 PATH
_GIT_CANDIDATES = (
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files\Git\bin\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
)


def _git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    for cand in _GIT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


FORBIDDEN_NAMES = {"secrets.toml", ".env", "persona.md", "privacy_terms.txt"}
FORBIDDEN_SUFFIX = {".key", ".pem", ".db", ".db-wal", ".db-shm"}
FORBIDDEN_DIRS = ("data/",)


def _tracked() -> list[str] | None:
    exe = _git()
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "-C", str(ROOT), "ls-files"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def main() -> int:
    files = _tracked()
    if files is None:
        # 不在 git 仓库里（例如解压出来的副本）：没东西可查，别假装通过
        print("[SKIP] 跟踪清单检查：当前目录不是 git 仓库")
        return 0

    bad: list[str] = []
    for f in files:
        name = f.rsplit("/", 1)[-1]
        if name in FORBIDDEN_NAMES or Path(name).suffix in FORBIDDEN_SUFFIX:
            bad.append(f"{f}  [禁用文件名/后缀]")
        elif any(f.startswith(d) for d in FORBIDDEN_DIRS):
            bad.append(f"{f}  [data/ 下不该进仓库]")

    if bad:
        print(f"[FAIL] 跟踪清单检查：{len(files)} 个文件里有 {len(bad)} 个不该被跟踪")
        for b in bad[:20]:
            print("   " + b)
        print("\n处理：git rm --cached <文件>，并确认 .gitignore 已经覆盖它。")
        return 1

    print(f"[PASS] 跟踪清单检查：{len(files)} 个文件，没有密钥/个人数据/数据库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
