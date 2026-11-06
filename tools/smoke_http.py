"""HTTP 链路的端到端冒烟测试（不打真实 API、不花 token）。

在进程内起一个假模型服务，把 base_url 指过去，跑三种场景：
1. 明确实体 + 回溯信号  -> 必须命中历史
2. 纯闲聊               -> 不检索
3. 工作任务             -> domain=work，且**不得注入热窗**（上下文隔离）

同时验证请求构造、JSON 模式、响应解析、用量统计、路由日志落库。

**它跑在真实库的一份副本上**（`data/_http_smoke.db`，用 SQLite 备份 API 复制）：
这个脚本要走完整对话链路，因此会写消息和路由日志。早先它直接写真实库，
于是每跑一次，`实时对话 <日期>` 里就多几条测试消息、路由日志也多几条——
"跑一次冒烟"污染了生产数据，还让后来的误查/漏查统计失真。副本用完即删。

用法：python tools/smoke_http.py
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from tools.mock_llm import Handler  # noqa: E402

from pmem import chat, db, llm  # noqa: E402
from pmem.config import load  # noqa: E402

PORT = 8791


def _copy_db(src: Path, dst: Path) -> None:
    """用 SQLite 备份 API 复制，避免直接复制 .db 拿到半写状态。"""
    for p in (dst, Path(str(dst) + "-wal"), Path(str(dst) + "-shm")):
        p.unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def main() -> int:
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    cfg = load()
    cfg.data["llm"]["base_url"] = f"http://127.0.0.1:{PORT}"

    real = cfg.path("paths", "db")
    tmp = real.parent / "_http_smoke.db"
    _copy_db(real, tmp)
    con = db.open_db(tmp, force=True)
    provider = llm.HttpProvider(cfg, api_key="sk-mock")
    sess = chat.ChatSession(con, cfg, provider)

    print(f"provider={provider.name}  base_url={provider.base_url}")
    print(f"库：{tmp.name}（真实库的副本，跑完删除；真实库不会被写）")
    print("=" * 78)

    cases = [
        ("我上次对000000是怎么说的", dict(need_recall=True, hits_min=1, domain="casual")),
        ("今天有点累", dict(need_recall=False, hits_min=0, domain="casual")),
        ("帮我评估一下这篇论文的技术路线", dict(need_recall=False, hits_min=0, domain="work")),
    ]

    ok = True
    for text, expect in cases:
        r = sess.respond(text)
        has_hot = "最近对话（热记忆）" in r.reply
        print(f"\n你> {text}")
        print(f"它> {r.reply}")
        # retrieved = 检索命中数（门之前）；hits = 门放行给生成器的那几条
        print(f"   domain={r.decision.domain} recall={r.decision.need_recall} "
              f"kw={r.decision.keywords} retrieved={r.retrieved} used={len(r.hits)} "
              f"hot_injected={has_hot} tok={r.decision.tokens_in}/{r.decision.tokens_out}")

        if r.decision.domain != expect["domain"]:
            print("   !! domain 不符"); ok = False
        if r.decision.need_recall != expect["need_recall"]:
            print("   !! need_recall 不符"); ok = False
        # 断言"检索到了"，不是"门放行了"：假模型的 JSON 回复里没有 sufficient 字段，
        # 所以门一定会判不足、used 必为空。那是 route/gate 的正常行为，不是本脚本要测的东西。
        if r.retrieved < expect["hits_min"]:
            print(f"   !! 命中数不足（retrieved={r.retrieved}）"); ok = False
        if expect["domain"] == "work" and has_hot:
            print("   !! 工作任务竟然注入了热窗（上下文隔离失效）"); ok = False

    rows = con.execute(
        "SELECT input_preview, domain, need_recall, hits, tokens_in, source "
        "FROM routing_log ORDER BY id DESC LIMIT 3"
    ).fetchall()
    print("\n最近 3 条路由日志：")
    for r in rows:
        print("  ", dict(r))
    # mock 每次调用都报 prompt_tokens=1234；而一轮里可能调用 2–3 次
    # （分类 + 第三档复核 + 生成），routing_log 记的是**这一轮的合计**，
    # 所以正确断言是"1234 的正整数倍"，而不是"恰好 1234"——
    # 后者在加了第三档复核（1234×3）之后就一直假红。
    bad = [r["tokens_in"] for r in rows
           if not r["tokens_in"] or r["tokens_in"] % 1234 != 0]
    if bad:
        print(f"!! 用量统计没有落库（异常值 {bad}）"); ok = False

    srv.shutdown()
    con.close()
    # 跑完即删；就算这次异常退出，下次启动时 _copy_db 也会先清掉残留
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    print("\nHTTP 链路:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
