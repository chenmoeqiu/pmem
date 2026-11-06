"""P4 对外出口的**跨进程**验收：一个"假运行时"，只通过命令行和 pmem 打交道。

为什么必须跨进程：`tools/smoke_task.py` 是直接 import 函数，绕过了命令行边界。
而真实调用方（DSH 或任何 agent）只能走命令行。边界上才会暴露：
输出编码、参数契约（能不能传时间）、只读性、静音是否真的生效、空结果怎么措辞。

假运行时做的事和真运行时完全一样，三步循环：

    context "<用户这句话>"   →  messages（self-contained，可直接发给模型）
    把 messages 发给模型     →  回复（本测试用固定桩，不调 API、不花钱）
    note "<用户这句话>" / note "<回复>"

断言 8 项。全部**重定向到文件、不开管道**——受限环境下管道可能被拒，
而"重定向时输出是 UTF-8"恰恰就是要验的第 1 条。

用法：python tools/smoke_ports.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pmem import clock, db, store  # noqa: E402
from pmem.config import load  # noqa: E402


class Runtime:
    """假运行时：只会调命令行，不会 import pmem。"""

    def __init__(self, db_path: Path, workdir: Path):
        self.db = db_path
        self.workdir = workdir
        self.n = 0

    def call(self, *args: str) -> tuple[int, str, str]:
        self.n += 1
        out = self.workdir / f"out_{self.n}.txt"
        err = self.workdir / f"err_{self.n}.txt"
        with open(out, "wb") as fo, open(err, "wb") as fe:
            p = subprocess.run(
                [sys.executable, "-m", "pmem", "--db", str(self.db), "--force", *args],
                cwd=str(ROOT), stdout=fo, stderr=fe, timeout=180,
            )
        so = out.read_bytes().decode("utf-8", errors="replace")
        se = err.read_bytes().decode("utf-8", errors="replace")
        return p.returncode, so, se

    def context(self, text: str) -> tuple[int, dict, str]:
        code, so, se = self.call("context", text, "--json", "--offline")
        try:
            return code, json.loads(so), se
        except Exception:
            return code, {}, se or so

    def note(self, text: str) -> tuple[int, dict, str]:
        code, so, se = self.call("note", text)
        try:
            return code, json.loads(so), se
        except Exception:
            return code, {}, se or so


def main() -> int:
    cfg = load()
    workdir = cfg.path("paths", "db").parent
    tmp = workdir / "_ports_smoke.db"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    # ---- 造数据：一场"正常的"会话由运行时自己 note 出来；
    #      另一场是"默认不调用"的创作对话，直接建好并静音。
    con = db.open_db(tmp, force=True)
    muted = store.upsert_conversation(
        con, source="takeout", source_id="ports-smoke-muted",
        title="科幻创作（验收用）", started_at=clock.now_utc_iso(),
        time_precision="message")
    store.add_message(
        con, conversation_id=muted, role="user", origin="user",
        content="差分机宇宙：靠差分机实现无限能源，但散热红线卡在 100TW",
        created_at=clock.now_utc_iso(), time_precision="message", source="takeout")
    con.execute("UPDATE conversations SET no_recall = 1, recall_triggers = '差分机' "
                "WHERE id = ?", (muted,))

    def counts() -> tuple[int, int, int]:
        q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
        return q("SELECT count(*) FROM messages"), q("SELECT count(*) FROM segments"), \
            q("SELECT count(*) FROM conversations")

    before = counts()
    con.close()

    rt = Runtime(tmp, workdir)
    results: list[tuple[str, bool, str]] = []

    # 1. 外部进程能跑通；重定向输出是合法 UTF-8 JSON
    code, ctx, err = rt.context("我上次说那台相机是什么型号")
    ok = (code == 0 and isinstance(ctx.get("messages"), list)
          and {"hot_text", "hits_text", "domain", "need_recall", "gate_sufficient"} <= set(ctx))
    results.append(("外部进程能跑通且输出合法 JSON", ok,
                    f"exit={code} keys={len(ctx)}" + ("" if ok else f" err={err[:60]}")))

    # 2. messages 自包含：system 打底 + 最后一条就是用户这句话（调用方不需要自己的历史）
    msgs = ctx.get("messages") or []
    roles = [m.get("role") for m in msgs]
    ok = (bool(msgs) and roles[-1] == "user"
          and msgs[-1].get("content") == "我上次说那台相机是什么型号"
          and "system" in roles and set(roles) <= {"system", "user"})
    results.append(("messages 自包含，可直接发给模型", ok, f"roles={roles}"))

    # 3. 只读：多次 context 不写消息
    for t in ("再问一个", "第三个问题"):
        rt.context(t)
    con = db.open_db(tmp, force=True)
    after = counts()
    con.close()
    results.append(("context 不写消息", before[0] == after[0],
                    f"messages {before[0]}→{after[0]}"))
    # 会把"实时对话"这行建出来，属于已知行为，单独报告不当失败
    note_conv = after[2] - before[2]

    # 4. 调用方无权传时间
    code, _so, se = rt.call("note", "试图指定时间", "--created-at", "2020-01-01T00:00:00Z")
    results.append(("note 拒绝调用方传时间", code != 0,
                    f"exit={code}" + ("" if code else "，居然接受了")))

    # 5. note 由 pmem 盖章，且下一轮 context 能在热窗里看到（热窗延续）
    code, n1, se = rt.note("端口验收标记：明早九点看晶圆厂的报价")
    stamped = bool(n1.get("created_at")) and n1.get("created_at", "").endswith("Z")
    _c2, ctx2, _ = rt.context("今天还有什么安排")
    visible = "端口验收标记" in (ctx2.get("hot_text") or "")
    results.append(("note 盖章 + 热窗延续", code == 0 and stamped and visible,
                    f"stamped={stamped} visible={visible}"))

    # 6. 静音会话不漏进上下文；点到触发词才放行
    _c3, ctx3, _ = rt.context("最近有什么新技术进展吗")
    blob = json.dumps(ctx3, ensure_ascii=False)
    leak = "差分机" in blob
    _c4, ctx4, _ = rt.context("差分机那个设定还算得通吗")
    released = "差分机" in (ctx4.get("hot_text") or "")
    results.append(("静音的会话不漏进上下文", not leak, "未泄漏" if not leak else "泄漏了"))
    results.append(("点到触发词才放行", released,
                    "命中" if released else "仍被挡住"))

    # 6b. 反向对照：关掉静音就必须能看见。否则上一条可能是"测试本身是空的"，
    #     修好过滤之前 hot_window 正是漏的（实测过）。
    con = db.open_db(tmp, force=True)
    from pmem import search  # noqa: PLC0415
    raw = search.hot_window(con, cfg, hours=24, include_muted=True)
    con.close()
    seen = any("差分机" in (m.get("content") or "") for m in raw)
    results.append(("反向对照：不静音时确实会带进来", seen,
                    "对照成立" if seen else "对照失效（说明上一条测不出问题）"))

    # 7. 查不到时说查不到，而不是给个空壳
    _c5, ctx5, _ = rt.context("zzqx不可能存在的词组qwe")
    ok = ctx5.get("retrieved") == 0 and not (ctx5.get("hits_text") or "").strip()
    results.append(("查不到就明说（hits 为空）", ok,
                    f"retrieved={ctx5.get('retrieved')} gate={ctx5.get('gate_sufficient')}"))

    # 8. 上下文预算：字符数不该超过 hot/hit 预算的量级（中文约 1 字≈1 token，给 2 倍余量）
    budget = 2 * ((int(cfg.get("chat", "hot_max_tokens", default=0)) or 1200)
                  + 8 * int(cfg.get("chat", "hit_max_tokens", default=300)))
    size = sum(len(m.get("content") or "") for m in msgs)
    results.append(("上下文在预算内", size <= budget, f"{size} ≤ {budget} 字"))

    # 9. IM 会话：同一 (source, session) 稳定落到同一场；不同 session 分开
    def _note_im(txt: str, src: str, sess: str) -> dict:
        _c, so, _e = rt.call("note", txt, "--source", src, "--session", sess)
        try:
            return json.loads(so)
        except Exception:
            return {}

    a1 = _note_im("你好，我在路上", "qq", "friend-1")
    a2 = _note_im("再说一句车上的事", "qq", "friend-1")
    b1 = _note_im("另一个会话", "qq", "friend-2")
    same = (a1.get("conversation_id") and
            a1.get("conversation_id") == a2.get("conversation_id"))
    apart = (b1.get("conversation_id") and
             b1.get("conversation_id") != a1.get("conversation_id"))
    results.append(("IM 同会话落到同一场", bool(same),
                    f"qf-1 → {a1.get('conversation_id')}/{a2.get('conversation_id')}"))
    results.append(("不同会话分开落库", bool(apart),
                    f"qf-2 → {b1.get('conversation_id')} ≠ {a1.get('conversation_id')}"))

    _c, so, _e = rt.call("context", "刚才车上说了什么", "--json", "--offline",
                         "--source", "qq", "--session", "friend-1")
    try:
        ctx_im = json.loads(so)
    except Exception:
        ctx_im = {}
    seen = "再说一句" in (ctx_im.get("hot_text") or "")
    results.append(("IM 的 context 能看到本会话", bool(seen),
                    f"hot 里{'有' if seen else '没有'}刚才那句"))

    # 10. IM 收图：note --attach 把附件挂到那条消息上
    img = workdir / "_port_attach.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
    _c, so, _e = rt.call("note", "看这张图", "--source", "qq", "--session", "friend-1",
                         "--attach", str(img))
    try:
        na = json.loads(so)
    except Exception:
        na = {}
    atts = na.get("attachments") or []
    results.append(("IM 附件能落到消息上",
                    bool(atts) and atts[0].get("new") is True and bool(na.get("message_id")),
                    f"附件 {len(atts)} 个"))

    _c, so, _e = rt.call("note", "这张不存在", "--source", "qq", "--session", "friend-1",
                         "--attach", str(workdir / "_nope.png"))
    try:
        nm = json.loads(so)
    except Exception:
        nm = {}
    results.append(("附件缺失不阻断消息",
                    bool(nm.get("message_id")) and bool(nm.get("attachment_errors")),
                    f"消息已写={bool(nm.get('message_id'))} "
                    f"错误 {len(nm.get('attachment_errors') or [])} 条"))

    _c, so, _e = rt.call("note", "再看一次同一张", "--source", "qq", "--session", "friend-1",
                         "--attach", str(img))
    try:
        nd = json.loads(so)
    except Exception:
        nd = {}
    dup = (nd.get("attachments") or [{}])[0].get("new")
    results.append(("同图不重复占空间", dup is False, f"new={dup}（False=复用同一份文件）"))
    img.unlink(missing_ok=True)

    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)
    for f in workdir.glob("out_*.txt"):
        f.unlink(missing_ok=True)
    for f in workdir.glob("err_*.txt"):
        f.unlink(missing_ok=True)

    width = max(len(r[0]) for r in results)
    ok_all = True
    print("P4 对外出口：跨进程（假运行时）\n")
    for name, ok, detail in results:
        if not ok:
            ok_all = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    if note_conv:
        print(f"\n  附注：context 顺手建了 {note_conv} 个会话行"
              f"（_live_conversation 的 upsert，不是写消息）")
    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
