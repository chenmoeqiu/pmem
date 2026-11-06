"""一条命令跑完全部验收（96 项上限）：verify + 8 个 smoke 套件 + 隐私扫描。

为什么要有这个文件：
"改完必须全跑"这句话，靠人记着的成本太高——而少跑一条，基线就不存在了。
这个脚本把整张验收清单变成一条命令，顺便给 CI 一个稳定入口。

判据只有两条，都很硬：
1. 每项的 **[PASS] 条数必须落在期望区间内**（见 `EXPECTED`；`verify` 在空库上有 2 项 SKIP）。
   条数对不上也算失败：断言被删掉时，脚本仍然会打印"全部通过"——那是假绿灯。
2. **任何一项没有输出也算失败**。静默通过是比失败更坏的情况。

用法：
    python tools/run_all.py              # 全部
    python tools/run_all.py --quiet      # 只打汇总
    python tools/run_all.py smoke_tag    # 只跑名字里含 smoke_tag 的（调试）
    python tools/run_all.py --list       # 列出会跑哪些

退出码：0 全过；1 有失败、条数不符或没有输出。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    print(f"需要 Python 3.11+（当前是 {sys.version.split()[0]}）：tomllib 在 3.11 才有。")
    print(f"换一个解释器再跑，例如：  py -3.12 {Path(__file__).name}")
    raise SystemExit(2)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent

# 每项的期望断言数。改断言时必须同步改这里，否则这条防线会悄悄失效。
# 写成 (下限, 上限) 表示"允许有 SKIP"：`verify` 在空库上会有 2 项 SKIP
# （没有探针词、没有附件），那不是失败，是"没有数据可验"。
EXPECTED = {
    "verify": (7, 9),
    "smoke_task": 8,
    "smoke_tag": 22,
    "smoke_ports": 16,
    "smoke_review": 12,
    "smoke_entity": 8,
    "smoke_profile": 7,
    "smoke_images": 7,
    "smoke_infer": 6,
    "check_privacy": 1,
}


def _bounds(name: str) -> tuple[int, int]:
    exp = EXPECTED.get(name, 0)
    if isinstance(exp, tuple):
        return int(exp[0]), int(exp[1])
    return int(exp), int(exp)

_PASS_RE = re.compile(r"\[PASS\]")
_FAIL_RE = re.compile(r"\[FAIL\]")


def steps() -> list[tuple[str, list[str]]]:
    """(名字, 命令)。清单**由 EXPECTED 决定**，不是「tools 下所有 smoke_*.py」。

    为什么不用通配：`tools/smoke_http.py` 是**手动**跑的联调脚本——它要求先在
    8791 端口起一个假模型服务，输出是给人看的叙述、没有 [PASS] 断言，
    服务没起时退出码是 -1。把它放进来自动跑，只会得到一个恒真的假绿灯。
    """
    out: list[tuple[str, list[str]]] = [
        ("verify", [sys.executable, "-m", "pmem", "verify"]),
    ]
    for name in sorted(EXPECTED):
        if name == "verify":
            continue
        p = ROOT / "tools" / f"{name}.py"
        if p.exists():
            out.append((name, [sys.executable, str(p)]))
        else:
            print(f"警告：EXPECTED 里有 {name}，但 tools/{name}.py 不存在")
    return out


def run_one(name: str, cmd: list[str]) -> tuple[int, int, str]:
    """返回 (pass 数, fail 数, 失败详情)。期望值在 main 里比对（可能是区间）。"""
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 0, 0, "超时（900 秒）"

    n_pass = len(_PASS_RE.findall(out))
    n_fail = len(_FAIL_RE.findall(out))
    detail = ""
    if n_fail:
        detail = "\n".join(
            line.strip() for line in out.splitlines() if "[FAIL]" in line
        )
    elif n_pass == 0:
        detail = (out.strip().splitlines() or ["（无输出）"])[-1][:200]
    return n_pass, n_fail, detail


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv or "-q" in argv
    filt = [a for a in argv[1:] if not a.startswith("-")]

    plan = steps()
    if filt:
        plan = [(n, c) for n, c in plan if any(f in n for f in filt)]
    if not plan:
        print("没有匹配的项。用 --list 看清单。")
        return 1

    if "--list" in argv:
        for name, _ in plan:
            lo, hi = _bounds(name)
            print(f"  {name:<14} 期望 {lo if lo == hi else f'{lo}-{hi}'} 项")
        return 0

    hi_total = sum(_bounds(n)[1] for n, _ in plan)
    print(f"pmem 验收全跑：{len(plan)} 项命令，合计期望 {hi_total} 项断言\n")

    total_pass = total_fail = 0
    bad: list[tuple[str, str]] = []

    for name, cmd in plan:
        n_pass, n_fail, detail = run_one(name, cmd)
        lo, hi = _bounds(name)
        total_pass += n_pass
        total_fail += n_fail

        if n_fail:
            status = "FAIL"
            bad.append((name, detail))
        elif not (lo <= n_pass <= hi):
            status = "COUNT"
            bad.append((name, f"断言数 {n_pass} 不在期望区间 {lo}-{hi}"))
        else:
            status = "ok"

        if not quiet:
            span = f"{lo}" if lo == hi else f"{lo}-{hi}"
            print(f"  [{status:^4}] {name:<14} {n_pass:>2}/{span}"
                  + ("" if status == "ok" else f"   {detail.splitlines()[0][:90] if detail else ''}"))

    print()
    print(f"断言：{total_pass} 项通过，{total_fail} 项失败（期望合计 {hi_total}）")

    if bad:
        print("\n未通过的命令：")
        for name, detail in bad:
            print(f"\n--- {name} ---")
            print(detail or "（无详情）")
        print("\n结果：失败")
        return 1

    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
