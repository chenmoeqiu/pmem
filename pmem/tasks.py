"""任务对象：只记录，不执行。

设计要点（见 docs/DESIGN.md 第 12 节"接入"）：
- 任务对象与执行器分离。谁跑由 `executor` 字段说明，pmem 不关心也不参与。
- 执行器中立的落地方式就是本模块的三个函数——任何进程（脚本 / DSH / 人）
  只要调用它们，就是一个合法执行器，不需要改 pmem 的任何代码。
- 任务的**过程**写进它自己的会话（source='task'），不进热窗；
  任务的**状态变更**（下发 / 完成）写一条消息到主对话，进热窗。
  这样"早上那个报告做完了吗"能直接从连续对话里答出来。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import clock, store

VALID_STATUS = ("queued", "running", "done", "failed", "cancelled")


def _announce(con: sqlite3.Connection, conversation_id: int, text: str) -> int:
    """往主对话写一条消息（进热窗）。这是任务与对话之间唯一的连接点。"""
    return store.add_message(
        con, conversation_id=conversation_id, role="assistant", origin="assistant",
        content=text, created_at=clock.now_utc_iso(),
        time_precision="message", source="task",
    )


def _ensure_worklog(con: sqlite3.Connection, task_id: int, goal: str) -> int:
    """给任务开一个工作日志会话（source='task'），热窗会把它排除。"""
    cid = store.upsert_conversation(
        con, source="task", source_id=f"task-{task_id}",
        title=f"任务 #{task_id}：{goal[:40]}",
        started_at=clock.now_utc_iso(), time_precision="message",
    )
    con.execute("UPDATE conversations SET task_id = ? WHERE id = ?", (task_id, cid))
    return cid


def start(
    con: sqlite3.Connection,
    *,
    goal: str,
    executor: str = "inline",
    parent_conversation_id: int | None = None,
    source_message_id: int | None = None,
    status: str = "running",
) -> dict[str, Any]:
    """登记一个任务。不执行它——执行是 executor 的事。"""
    if status not in VALID_STATUS:
        raise ValueError(f"status 必须是 {VALID_STATUS}")
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal 不能为空")
    if parent_conversation_id is not None:
        ok = con.execute("SELECT 1 FROM conversations WHERE id = ?",
                         (parent_conversation_id,)).fetchone()
        if not ok:
            raise ValueError(f"父对话 #{parent_conversation_id} 不存在（先有对话才能挂任务）")

    now = clock.now_utc_iso()
    cur = con.execute(
        """INSERT INTO tasks
               (goal, status, executor, parent_conversation_id, source_message_id,
                started_at, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (goal, status, executor, parent_conversation_id, source_message_id,
         now if status == "running" else None, now),
    )
    task_id = int(cur.lastrowid)

    cid = _ensure_worklog(con, task_id, goal)
    con.execute("UPDATE tasks SET conversation_id = ? WHERE id = ?", (cid, task_id))

    if parent_conversation_id is not None:
        _announce(con, parent_conversation_id,
                  f"已下发任务 #{task_id}（{goal[:50]}），执行器 `{executor}`，"
                  f"状态 {status}。")
    return get(con, task_id)


def finish(
    con: sqlite3.Connection,
    task_id: int,
    *,
    status: str = "done",
    summary: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """收尾。会往主对话写一条可见消息，让结果进入连续对话。"""
    if status not in ("done", "failed", "cancelled"):
        raise ValueError("finish 的 status 只能是 done / failed / cancelled")
    row = get(con, task_id)
    if row is None:
        raise ValueError(f"没有任务 #{task_id}")

    con.execute(
        "UPDATE tasks SET status = ?, result_summary = ?, error = ?, finished_at = ? "
        "WHERE id = ?",
        (status, summary, error, clock.now_utc_iso(), task_id),
    )

    parent = row.get("parent_conversation_id")
    if parent:
        if status == "done":
            text = f"任务 #{task_id} 已完成。"
            if summary:
                text += f"\n结论：{summary}"
        else:
            text = f"任务 #{task_id} {status}。"
            if error:
                text += f"\n原因：{error}"
        _announce(con, int(parent), text)
    return get(con, task_id)


def log(con: sqlite3.Connection, task_id: int, text: str) -> int:
    """往任务自己的工作日志里写一条（不进热窗）。执行器可以随时调。"""
    row = get(con, task_id)
    if row is None or not row.get("conversation_id"):
        raise ValueError(f"任务 #{task_id} 没有工作日志会话")
    return store.add_message(
        con, conversation_id=int(row["conversation_id"]), role="assistant",
        origin="assistant", content=text, created_at=clock.now_utc_iso(),
        time_precision="message", source="task",
    )


def run_inline(
    con: sqlite3.Connection,
    cfg,
    provider,
    task_id: int,
    *,
    dry: bool = False,
) -> dict[str, Any]:
    """内置执行器：一次模型调用产出一份结果。

    它是**执行器**，不是任务对象的一部分——`pmem` 不知道也不限制别人怎么写别的执行器。
    能力边界要诚实：它没有工具，不能读文件、不能上网，只能基于记忆与通用知识作答。
    需要工具的活，请用 `executor=agent:<名字>`，由那个程序调 start/log/finish。
    """
    from . import prompt, ports

    t = get(con, task_id)
    if t is None:
        raise ValueError(f"没有任务 #{task_id}")

    con.execute("UPDATE tasks SET status = 'running', started_at = ? WHERE id = ?",
                (clock.now_utc_iso(), task_id))

    ctx = ports.build_context(con, cfg, provider, t["goal"])
    # 任务必须用**任务专用**的系统提示，不能复用"记忆助手"的人设：
    # 后者会把"记忆里没有"当成"不能做"，于是连"写个模板"都拒绝。
    messages = prompt.build_task_messages(
        t["goal"], hits_text=ctx.get("hits_text", ""),
        now_line=prompt.current_time_line(),
    )

    if dry:
        return {"task_id": task_id, "dry": True, "messages": messages,
                "retrieved": ctx["retrieved"], "used": ctx["used"]}

    try:
        reply, usage = provider.synthesize(messages)
    except Exception as e:
        return finish(con, task_id, status="failed", error=f"{type(e).__name__}: {e}")

    log(con, task_id, reply)
    summary = reply.strip().replace("\n", " ")[:120]
    return finish(con, task_id, status="done", summary=summary)


def get(con: sqlite3.Connection, task_id: int) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return None
    d = {k: row[k] for k in row.keys()}
    off = 8.0
    d["created_at_local"] = clock.local_str(d["created_at"], off)
    d["finished_at_local"] = clock.local_str(d["finished_at"], off) if d["finished_at"] else None
    return d


def list_tasks(
    con: sqlite3.Connection, *, status: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM tasks"
    params: list[Any] = []
    if status and status != "all":
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    out = []
    for row in con.execute(sql, params).fetchall():
        d = {k: row[k] for k in row.keys()}
        d["created_at_local"] = clock.local_str(d["created_at"], 8)
        out.append(d)
    return out
