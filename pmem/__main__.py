import sys

# 版本守卫要放在 import cli 之前：cli 会一路 import 到 config -> tomllib，
# 而 tomllib 是 3.11 才有的。否则用户（或 run.cmd 的最后回退）拿到的是
# 一串 traceback，而不是"该换解释器"这句话。
if sys.version_info < (3, 11):
    print(f"pmem 需要 Python 3.11+（当前 {sys.version.split()[0]}）：tomllib 在 3.11 才有。")
    print("Windows 上可以试：  py -3.12 -m pmem <命令>")
    print("或者设置 PMEM_PYTHON 指向一个 3.11+ 的 python.exe 再用 run.cmd。")
    sys.exit(2)

from .cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
