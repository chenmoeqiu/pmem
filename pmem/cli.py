"""命令行入口。

设计原则：所有命令都不需要你懂 Python，复制粘贴即可。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import (__version__, chat as chatmod, clock, db, entities, export, gate, gaps, llm,
              purge, search, store, text)

_TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled")
_DIMENSIONS = ("主题", "地点", "人物", "机构", "任务", "情绪")
from .config import load
from .importers import takeout, voyager


def _setup_console() -> None:
    """Windows 中文终端是 GBK：交互时保留原编码，重定向时强制 UTF-8。

    否则把输出重定向到文件会 UnicodeEncodeError，而交互时强制 UTF-8
    又会让 GBK 终端显示乱码。
    """
    if sys.platform != "win32":
        return
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        else:
            sys.stdout.reconfigure(errors="replace")
            sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    try:
        # 管道输入时 stdin 默认按本地代码页解码，UTF-8 管道会被解成代理字符
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _emit(rows, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("（没有找到匹配的记忆）")
        return
    for r in rows:
        title = r.get("conversation_title") or "(无标题)"
        score = r.get("score")
        head = f"[{score:.3f}] " if isinstance(score, (int, float)) else ""
        stamp = r.get("created_at_local", "未知时间")
        print(f"\n{head}{stamp}  {r.get('role', '')}  《{title}》")
        if r.get("tag"):
            print(f"  标签：{r['tag']}")
        print(f"  {r.get('snippet', r.get('content', ''))}")


def cmd_init(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    v = con.execute("PRAGMA journal_mode").fetchone()[0]
    sv = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    print(f"数据库就绪：{db_path}")
    print(f"  schema 版本 {sv} / journal_mode={v} / sqlite {sqlite3.sqlite_version}")
    print(f"  媒体目录：{cfg.path('paths', 'media')}")
    return 0


def cmd_import(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    src = Path(args.source)
    if not src.exists():
        raise SystemExit(f"文件不存在：{src}")

    if args.kind == "takeout":
        takeout.import_takeout(
            con, cfg, src,
            dry_run=args.dry_run,
            limit=args.limit,
            skip_attachments=args.skip_attachments,
            skip_video=False if args.include_video else None,
            max_attachment_mb=args.max_attachment_mb,
        )
    else:
        voyager.import_voyager(con, cfg, src, at=args.at, date_only=args.date)
    return 0


def cmd_search(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    if getattr(args, "tag", None):
        rows = search.search_by_tag(con, cfg, args.tag,
                                    dimension=getattr(args, "dim", None),
                                    include_descendants=not args.exact,
                                    limit=args.limit or 10)
        if not rows:
            print(f"没有命中已批准标签「{args.tag}」的记忆。"
                  f"（待审值不参与检索，用 `pmem tag list --state pending` 看看）")
            return 0
        _emit(rows, args.json)
        return 0
    if getattr(args, "images", False):
        from . import images
        rows = images.search(con, args.query, limit=args.limit or 5)
        if not rows:
            print(f"没有描述匹配「{args.query}」的图片。"
                  f"（先用 `pmem describe --limit 5` 给图片写描述）")
            return 0
        for r in rows:
            print(f"\n  图 #{r['id']}  {r['created_at'][:16]}")
            print(f"    描述：{r['description']}")
            print(f"    来源：《{(r['conversation_title'] or '')[:30]}》  {r['path']}")
        return 0
    rows = search.search(con, cfg, args.query, limit=args.limit,
                         conversation_id=args.conversation)
    _emit(rows, args.json)
    return 0


def _resolve_conversation(con, ref: str) -> int | None:
    """会话定位：接受 id、source_id 或对话链接（Takeout 里每条都有 url）。"""
    ref = str(ref).strip()
    if ref.isdigit():
        row = con.execute("SELECT id FROM conversations WHERE id = ?", (int(ref),)).fetchone()
    else:
        row = con.execute(
            "SELECT id FROM conversations WHERE url = ? OR source_id = ?", (ref, ref)
        ).fetchone()
    return int(row["id"]) if row else None


def cmd_conv(args, cfg) -> int:
    """会话级开关：默认不调用（自己的创作草稿不该在无关话题里冒出来）。"""
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)

    if args.action == "list":
        rows = con.execute(
            "SELECT id, title, recall_triggers, url FROM conversations "
            "WHERE COALESCE(no_recall, 0) = 1 ORDER BY id"
        ).fetchall()
        if not rows:
            print("没有「默认不调用」的会话。")
            return 0
        for r in rows:
            trig = r["recall_triggers"] or "（无触发词：永不召回）"
            print(f"  #{r['id']:<5} {(r['title'] or '')[:30]:<30} 触发词={trig}")
            print(f"          {r['url'] or ''}")
        return 0

    cid = _resolve_conversation(con, args.id)
    if cid is None:
        raise SystemExit(f"找不到会话 {args.id}")

    if args.action == "mute":
        trig = ",".join(t.strip() for t in (args.triggers or "").split(",") if t.strip())
        con.execute(
            "UPDATE conversations SET no_recall = 1, recall_triggers = ? WHERE id = ?",
            (trig or None, cid),
        )
        print(f"会话 #{cid} 已设为默认不调用"
              + (f"，当前这句话出现「{trig}」才放行" if trig else "（无触发词 = 永不召回）"))
    else:
        con.execute(
            "UPDATE conversations SET no_recall = 0, recall_triggers = NULL WHERE id = ?",
            (cid,),
        )
        print(f"会话 #{cid} 已恢复调用")
    return 0


def cmd_profile(args, cfg) -> int:
    """用户状态：断言 + 有效期 + 指向原文的证据。只写状态层，原文不动。"""
    from . import profile
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)

    if args.action == "add":
        ev = [int(x) for x in args.evidence.replace("，", ",").split(",") if x.strip().isdigit()]
        r = profile.add_fact(con, args.kind, args.subject, args.value,
                             valid_from=args.valid_from, valid_to=args.valid_to,
                             evidence=ev, origin=args.origin)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if args.action == "list":
        rows = profile.list_facts(con, subject=args.subject, kind=args.kind,
                                  include_superseded=args.all, limit=args.limit)
        if not rows:
            print("还没有用户状态。用 `pmem profile add <kind> <subject> <value> "
                  "--evidence <消息id>` 添加。")
            return 0
        for r in rows:
            mark = "已确认" if r["origin"] == "confirmed" else "推断"
            when = f"{r['valid_from'] or '?'} → {r['valid_to'] or '今'}"
            print(f"  #{r['id']:<4} [{mark}] {r['kind']}/{r['subject']}：{r['value']}")
            print(f"         有效期 {when}｜证据 {r['evidence'] or '（无）'}"
                  + (f"｜已被 #{r['superseded_by']} 取代" if r["superseded_by"] else ""))
        return 0

    if args.action == "confirm":
        print(json.dumps(profile.confirm(con, args.id), ensure_ascii=False, indent=2))
        return 0

    if args.action == "history":
        rows = profile.history(con, args.kind, args.subject)
        if not rows:
            print("没有这条的历史。")
            return 0
        for r in rows:
            mark = "确认" if r["origin"] == "confirmed" else "推断"
            tail = f" → 被 #{r['superseded_by']} 取代" if r["superseded_by"] else "（当前）"
            print(f"  #{r['id']} [{mark}] {r['value']}  "
                  f"{r['valid_from'] or '?'}→{r['valid_to'] or '今'}{tail}")
        return 0

    if args.action == "export":
        r = profile.export_md(con, cfg, out_dir=args.out)
        print(f"导出 {r['facts']} 条到 {r['dir']}")
        for name in r["files"]:
            print(f"  {name}")
        if r["facts"] == 0:
            print("（表里还没有状态，所以只写了一个空索引）")
        return 0

    raise SystemExit("未知的 profile 子命令")


def cmd_entity(args, cfg) -> int:
    """实体层：人物/机构 + 归属区间。只写派生层，原文不动。"""
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)

    if args.action == "sync":
        print(json.dumps(entities.sync_from_tags(con), ensure_ascii=False, indent=2))
        return 0

    if args.action == "list":
        sql = """SELECT e.id, e.kind, e.name, e.is_role,
                        (SELECT COUNT(*) FROM entity_mentions m WHERE m.entity_id = e.id) hits
                   FROM entities e"""
        where, params = [], []
        if args.kind:
            where.append("e.kind = ?")
            params.append(args.kind)
        if args.roles:
            where.append("e.is_role = 1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY hits DESC, e.name LIMIT ?"
        params.append(args.limit)
        rows = con.execute(sql, params).fetchall()
        if not rows:
            print("还没有实体。先跑 `pmem entity sync`（把人物/机构标签投影过来）。")
            return 0
        for r in rows:
            mark = "角色" if r["is_role"] else ("机构" if r["kind"] == "org" else "具名")
            print(f"  #{r['id']:<4} {mark}  {r['name']:<14} 提到 {r['hits']} 段")
        return 0

    if args.action == "show":
        row = con.execute(
            "SELECT * FROM entities WHERE name = ? OR id = ? ORDER BY id LIMIT 1",
            (args.name, int(args.name) if str(args.name).isdigit() else -1)).fetchone()
        if not row:
            raise SystemExit(f"没有实体 {args.name}")
        hits = con.execute("SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ?",
                           (row["id"],)).fetchone()[0]
        print(f"#{row['id']}  {row['name']}  kind={row['kind']} "
              f"is_role={bool(row['is_role'])}  提到 {hits} 段")
        ten = con.execute(
            """SELECT t.*, o.name AS org FROM entity_tenure t
                 LEFT JOIN entities o ON o.id = t.org_id
                WHERE t.entity_id = ? ORDER BY t.id""", (row["id"],)).fetchall()
        if not ten:
            print("  （没有归属区间——未知就留空，不假装知道）")
        for t in ten:
            print(f"  #{t['id']}  区间：{t['org'] or '（未指定机构）'}  "
                  f"{t['valid_from'] or '?'} → {t['valid_to'] or '今'}  "
                  f"origin={t['origin']}")
            if t["basis"]:
                print(f"         依据：「{t['basis']}」")
            if t["evidence"] and t["evidence"] != "[]":
                print(f"         原文消息：{t['evidence']}")
        return 0

    if args.action == "tenure-confirm":
        print(json.dumps(entities.confirm_tenure(con, args.id),
                         ensure_ascii=False, indent=2))
        return 0

    if args.action == "tenure-del":
        row = con.execute("SELECT id, origin FROM entity_tenure WHERE id = ?",
                          (args.id,)).fetchone()
        if not row:
            raise SystemExit(f"没有区间 #{args.id}")
        con.execute("DELETE FROM entity_tenure WHERE id = ?", (args.id,))
        print(f"已删除区间 #{args.id}（原 origin={row['origin']}）")
        return 0

    if args.action == "tenure":
        r = entities.add_tenure(con, args.person, args.org,
                                valid_from=args.valid_from, valid_to=args.valid_to,
                                origin=args.origin)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if args.action == "candidates":
        rows = entities.candidates_at(con, args.when, org=args.org,
                                      only_roles=args.roles)
        if not rows:
            print("这个时刻没有可用的候选（要么没登记区间，要么区间不覆盖）。")
            return 0
        print(f"候选 {len(rows)} 个——**这只是候选，不是结论**：")
        for r in rows:
            print(f"  #{r['person_id']:<4} {r['person']:<12} @ {r['org'] or '?'}  "
                  f"{r['valid_from'] or '?'} → {r['valid_to'] or '今'}")
        return 0

    if args.action == "unresolved":
        rows = entities.unresolved_roles(con, limit=args.limit)
        if not rows:
            print("没有需要人确认的角色实体。")
            return 0
        print(f"{len(rows)} 个角色实体需要人确认（只列出来，不写结论）：")
        for r in rows:
            print(f"  #{r['entity_id']:<4} {r['name']:<12} 提到 {r['hits']} 段 · {r['why']}")
        return 0

    if args.action == "ask":
        from . import review
        n = 0
        for u in entities.unresolved_roles(con, limit=args.limit):
            nid = review.add_pending(con, "entity", {
                "source": "entity",
                "sig_key": f"entity:{u['entity_id']}",
                "entity_id": u["entity_id"],
                "topic": f"「{u['name']}」（提到 {u['hits']} 段）",
                "old": u["why"],
                "new": "",
                "why": f"「{u['name']}」是同一个人，还是不同时期的几个不同的人？",
            })
            if nid:
                n += 1
        print(f"新增 {n} 条实体问题（已有的不重复报）。用 `pmem pending list` 看。")
        return 0

    raise SystemExit("未知的 entity 子命令")


def cmd_review(args, cfg) -> int:
    """复查：每周找矛盾 / 每月重整理后对比。只记录，不改记忆。"""
    from . import review
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    provider = llm.build(cfg, force_offline=args.offline)
    if args.action == "weekly":
        r = review.weekly(con, cfg, provider, days=args.days, limit=args.limit)
    else:
        r = review.monthly(con, cfg, provider, limit=args.limit)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if r.get("new"):
        print(f"\n已记入待裁决队列 {r['new']} 条。用 `pmem pending list` 看，"
              f"`pmem pending answer <id> \"你的说明\"` 逐条回复。")
    return 0


def cmd_pending(args, cfg) -> int:
    """人在回路：看、答、关。**不催促**——什么时候处理由你决定。"""
    from . import review
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)

    if args.action == "list":
        rows = review.list_pending(con, status=args.status, limit=args.limit)
        if not rows:
            print(f"没有 {args.status} 状态的待裁决项。")
            return 0
        for r in rows:
            d = r["detail"]
            tag = {"contradiction": "矛盾", "drift": "漂移",
                   "entity": "实体"}.get(r["kind"], r["kind"])
            print(f"\n#{r['id']}  [{tag}]  {r['created_at'][:16]}")
            print(f"   话题：{d.get('topic') or '（未标注）'}")
            print(f"   更早：{d.get('old') or '—'}")
            print(f"   最近：{d.get('new') or '—'}")
            if d.get("why"):
                print(f"   为什么：{d['why']}")
            if r.get("answer"):
                print(f"   我的回答：{r['answer']}")
            if r.get("proposal"):
                from . import actions as _actions
                try:
                    act = json.loads(r["proposal"])
                except Exception:
                    act = {}
                print(f"   建议动作：{_actions.describe(act)}")
        print(f"\n共 {len(rows)} 条。回复：pmem pending answer <id> \"说明\"；"
              f"提议动作：pmem pending propose <id>；执行：pmem pending apply <id>")
        return 0

    if args.action == "answer":
        print(json.dumps(review.answer(con, args.id, args.text),
                         ensure_ascii=False, indent=2))
        return 0

    if args.action == "propose":
        from . import actions, llm
        prov = llm.build(cfg, force_offline=args.offline)
        r = actions.propose(con, cfg, prov, args.id)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1

    if args.action == "apply":
        from . import actions
        r = actions.apply(con, args.id, actor="cli")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1

    if args.action == "reject":
        from . import actions
        r = actions.reject(con, args.id, args.reason)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1

    if args.action == "log":
        from . import actions
        rows = actions.log(con, args.limit)
        if not rows:
            print("还没执行过任何动作。")
            return 0
        for r in rows:
            print(f"  #{r['id']} {r['created_at'][:16]}  {r['kind']:<18} "
                  f"item={r['item_id']} actor={r['actor']}")
            print(f"      {json.dumps(r['result'], ensure_ascii=False)[:120]}")
        return 0

    if args.action in ("resolve", "dismiss", "reopen"):
        status = {"resolve": "resolved", "dismiss": "dismissed", "reopen": "open"}[args.action]
        print(json.dumps(review.set_status(con, args.id, status),
                         ensure_ascii=False, indent=2))
        return 0

    raise SystemExit("未知的 pending 子命令")


def cmd_daily(args, cfg) -> int:
    """闲时整理：把转冷的会话切段 + 打标。

    正常情况下这一步**不需要你敲**——一轮对话结束时会自动跑一点点
    （见 `pmem/daily.py`）。这个命令是给它一个可定时、可强制的入口：
    接系统任务计划、或手动补跑。
    """
    from . import daily, llm
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)

    before = daily.pending_count(con)
    ok, why = daily.due(con, cfg)
    print(f"转冷队列：{before} 场待整理；判定：{'该跑' if ok else '先不跑'}（{why}）")

    if args.dry:
        from . import tagging as _tagging
        print(f"前 10 场：{_tagging.pending_cold(con, limit=10)}")
        return 0

    provider = llm.build(cfg, force_offline=args.offline)
    if args.now:
        r = daily.run(con, cfg, provider, limit=args.limit)
    else:
        if not ok:
            print("没有需要做的（用 --now 可忽略间隔限制）。")
            return 0
        r = daily.run(con, cfg, provider, limit=args.limit)

    if not r.get("ok"):
        print(r.get("reason") or "没跑动。")
        return 1
    print(f"本次处理 {r['processed']} 场（失败 {r['failed']}）"
          f"，新增段 {r['segments']} / 标签关联 {r['tags']}"
          f"；队列还剩 {r['pending']} 场")
    return 0


def cmd_describe(args, cfg) -> int:
    """给图片写一句话描述。默认只处理少量，不扫全库。"""
    from . import images
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    ids = [int(x) for x in args.id.replace("，", ",").split(",")
           if x.strip().isdigit()] if args.id else None
    r = images.describe_batch(con, cfg, llm.build(cfg, force_offline=args.offline),
                              limit=args.limit, ids=ids)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if r["described"]:
        print(f"\n可用 `pmem search --images <关键词>` 查这些描述。")
    return 0


def cmd_infer(args, cfg) -> int:
    """从原文推断（只写 derived，带证据，不覆盖已确认）。"""
    from . import infer
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    r = infer.run(con, cfg, llm.build(cfg, force_offline=args.offline),
                  limit=args.limit, dry=args.dry)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if r["written"]:
        print(f"\n推断出 {r['written']} 条任职区间（origin=derived，等你确认）。\n"
              f"看：`pmem entity show 我`；确认：`pmem profile confirm` 只对状态条生效，"
              f"区间可用 `pmem entity tenure` 手改。")
    return 0


def cmd_sleep(args, cfg) -> int:
    """手动补一个"这里睡了一觉"的边界。

    按作息自动判断会误判（熬夜、跨时区、白天补觉），所以留这个手动出口：
    标了之后，热记忆从这一刻重新开始，之前的都转成可检索的冷记忆。
    """
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    if args.list:
        rows = con.execute(
            "SELECT id, at, note FROM sleep_marks ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            print("还没有手动标记的睡眠点。")
            return 0
        for r in rows:
            print(f"  #{r['id']:<4} {clock.local_str(r['at'])}  {r['note'] or ''}")
        return 0
    at = args.at or clock.now_utc_iso()
    con.execute("INSERT INTO sleep_marks(at, note, created_at) VALUES (?,?,?)",
                (at, args.note, clock.now_utc_iso()))
    since = search.segment_start_iso(con, cfg)
    print(f"已标记睡眠点：{clock.local_str(at)}")
    print(f"当前热记忆从这里开始：{clock.local_str(since) if since else '（无边界，走回退窗口）'}")
    return 0


def cmd_show(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    m = search.get_message(con, cfg, args.id)
    if not m:
        raise SystemExit(f"找不到消息 {args.id}")
    print(f"《{m['conversation_title']}》  第 {m['seq']} 条  {m['created_at_local']}  "
          f"role={m['role']} origin={m['origin']} 精度={m['time_precision']}")
    print("-" * 72)
    print(m["content"])
    for a in m["attachments"]:
        print(f"\n附件 {a['id']}: {a['orig_name']} ({a['bytes']} B, {a['kind']})\n  {a['path']}")
    return 0


def cmd_hot(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    rows = search.hot_window(con, cfg, hours=args.hours, include_muted=True)
    if not rows:
        print(f"最近 {args.hours} 小时没有消息。")
        return 0
    for r in rows:
        print(f"[{r['created_at_local']}] {r['role']}: {r['content'][:80]}")
    print(f"\n共 {len(rows)} 条（这就是热记忆：整段注入，不检索）")
    return 0


def cmd_stats(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    s = search.stats(con, cfg)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    print(f"对话      : {s['conversations']}")
    print(f"消息      : {s['messages']}  {s['by_role']}")
    print(f"时间精度  : {s['by_time_precision']}")
    print(f"时间范围  : {s['first_message_local']}  ->  {s['last_message_local']}")
    print(f"附件      : {s['attachments']} 个 / {s['attachment_mb']} MB")
    g = gaps.summary(con)
    if g:
        print(f"导入缺口  : {g}")
    routes = con.execute("SELECT COUNT(*) FROM routing_log").fetchone()[0]
    if routes:
        print(f"路由记录  : {routes} 条")
    if s["purged_messages"]:
        print(f"已清除    : {s['purged_messages']} 条")
    return 0


def cmd_export(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    out = Path(args.out) if args.out else cfg.path("paths", "export")
    fmt = args.format
    if fmt in ("jsonl", "both"):
        p = out / "memory.jsonl"
        n = export.export_jsonl(con, cfg, p)
        print(f"JSONL: {p}  ({n} 条)")
    if fmt in ("md", "both"):
        d = out / "markdown"
        n = export.export_markdown(con, cfg, d)
        print(f"Markdown: {d}  ({n} 个文件)")
    return 0


def cmd_purge(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    media = cfg.path("paths", "media")
    if args.message:
        r = purge.purge_message(con, media, args.message, hard_files=args.hard, note=args.note)
    elif args.conversation:
        r = purge.purge_conversation(con, media, args.conversation,
                                     hard_files=args.hard, note=args.note)
    else:
        raise SystemExit("需要 --message ID 或 --conversation ID")
    print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_ask(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    provider = llm.build(cfg, force_offline=args.offline)
    sess = chatmod.ChatSession(con, cfg, provider)
    r = sess.respond(args.text, force_recall=args.recall)

    if args.json:
        print(json.dumps({
            "kind": r.kind,
            "reply": r.reply,
            "domain": r.decision.domain,
            "need_recall": r.decision.need_recall,
            "keywords": r.decision.keywords,
            "ambiguous": r.decision.ambiguous,
            "source": r.decision.source,
            "reason": r.decision.reason,
            "hits": len(r.hits),
            "latency_ms": r.decision.latency_ms,
            "provider": provider.name,
            "gate": None if r.gate is None else {
                "sufficient": r.gate.sufficient,
                "used": r.gate.used,
                "missing": r.gate.missing,
                "next": r.gate.next_action,
            },
        }, ensure_ascii=False, indent=2))
        return 0

    print(r.reply)
    print()
    print(f"  · 路由：{r.decision.domain}  need_recall={r.decision.need_recall} "
          f"关键词={r.decision.keywords or '无'} 来源={r.decision.source}")
    print(f"  · 类型：{r.kind}  召回 {r.retrieved or len(r.hits)} 条 / 采用 {len(r.hits)} 条  "
          f"用时 {r.decision.latency_ms}ms  provider={provider.name}")
    if r.gate is not None:
        state = "充足" if r.gate.sufficient else "不足"
        used = r.gate.used or []
        print(f"  · 证据门：{state}  采用 {used or '无'} 条  next={r.gate.next_action}")
        if r.gate.missing:
            print(f"  · 缺口：{r.gate.missing}")
    if r.decision.reason:
        print(f"  · 理由：{r.decision.reason}")
    return 0


def cmd_chat(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    provider = llm.build(cfg, force_offline=args.offline)
    sess = chatmod.ChatSession(con, cfg, provider)

    mode = "离线（无生成）" if provider.name == "offline" else "在线"
    print(f"pmem chat · {mode} · /quit 退出 · /recall <词> 强制检索 · /stats 统计")
    while True:
        try:
            line = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            return 0
        if line == "/stats":
            s = search.stats(con, cfg)
            print(f"对话 {s['conversations']} / 消息 {s['messages']} / 附件 {s['attachments']}")
            continue
        if line.startswith("/recall "):
            r = sess.recall(line[len("/recall "):].strip())
            print("\n" + r.reply)
            continue
        try:
            r = sess.respond(line)
        except Exception as e:
            print(f"\n[出错] {type(e).__name__}: {e}")
            continue
        print("\n" + r.reply)
        print(f"  · {r.decision.domain} · 命中 {len(r.hits)} 条 · {r.decision.latency_ms}ms")


def cmd_gaps(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    rows = gaps.list_gaps(con, status=args.status, limit=args.limit)
    s = gaps.summary(con)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"没有 {args.status} 状态的导入缺口。")
        return 0
    print(f"导入缺口（{args.status}）：共 {len(rows)} 条  "
          f"统计 {s}\n")
    for r in rows:
        title = (r["conversation_title"] or "")[:24]
        print(f"  #{r['id']:<5} msg={r['message_id']:<6} {r['reason']:<15} "
              f"{r['orig_name'][:40]:<42} 《{title}》")
    print("\n补一个文件：pmem attach <消息id> <本地文件路径>")
    return 0


def cmd_attach(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    r = gaps.attach_manual(con, cfg, message_id=args.message_id,
                           file_path=Path(args.file))
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


def cmd_log(args, cfg) -> int:
    from . import route
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    rows = route.recent_logs(con, limit=args.last)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("还没有路由记录。")
        return 0
    for r in reversed(rows):
        g = r.get("gate_sufficient")
        gate_txt = "未启用" if g is None else ("充足" if g else "不足")
        print(f"\n[{r['created_at_local']}] {r['domain']:<6} "
              f"recall={r['need_recall']} hits={r['hits']} src={r['source']} "
              f"证据门={gate_txt}")
        print(f"  输入：{r['input_preview']}")
        print(f"  关键词：{r['keywords']}  理由：{r['reason']}")
        if g is not None:
            print(f"  门：used={r.get('gate_used')} missing={r.get('gate_missing') or '—'}")
    return 0


def cmd_context(args, cfg) -> int:
    from . import ports
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    provider = llm.build(cfg, force_offline=args.offline)
    r = ports.build_context(con, cfg, provider, args.text,
                            source=getattr(args, "source", None),
                            session=getattr(args, "session", None))

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print(f"# 路由：{r['domain']} recall={r['need_recall']} "
          f"关键词={r['keywords'] or '无'} 召回 {r['retrieved']} 条 / 采用 {r['used']} 条")
    if r["gate_sufficient"] is not None:
        print(f"# 证据门：{'充足' if r['gate_sufficient'] else '不足'} "
              f"{r['gate_missing'] or ''}".rstrip())
    print()
    for m in r["messages"]:
        print(f"--- {m['role']} ---")
        print(m["content"])
        print()
    return 0


def cmd_note(args, cfg) -> int:
    from . import ports
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    r = ports.note(con, cfg, text=args.text, role=args.role,
                   conversation_id=args.conversation,
                   source=getattr(args, "source", None),
                   session=getattr(args, "session", None),
                   attachments=getattr(args, "attach", None),
                   describe=bool(getattr(args, "describe", False)),
                   provider=llm.build(cfg, force_offline=getattr(args, "offline", False)))
    print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_task(args, cfg) -> int:
    from . import tasks
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)

    if args.action == "start":
        r = tasks.start(con, goal=args.goal, executor=args.executor,
                        parent_conversation_id=args.parent,
                        status=args.status)
    elif args.action == "finish":
        r = tasks.finish(con, args.task_id, status=args.status,
                         summary=args.summary, error=args.error)
    elif args.action == "log":
        n = tasks.log(con, args.task_id, args.text)
        print(f"已写入任务 #{args.task_id} 的工作日志（消息 {n}）")
        return 0
    elif args.action == "run":
        r = tasks.run_inline(con, cfg, llm.build(cfg, force_offline=args.offline),
                             args.task_id, dry=args.dry)
        if args.dry:
            print(json.dumps({k: v for k, v in r.items() if k != "messages"},
                             ensure_ascii=False, indent=2))
            print("\n（dry-run：下面是要发给模型的内容，没有实际调用）\n")
            for m in r["messages"]:
                print(f"--- {m['role']} ---\n{m['content']}\n")
            return 0
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    else:  # list
        rows = tasks.list_tasks(con, status=args.status, limit=args.limit)
        if not rows:
            print("没有任务。")
            return 0
        for t in rows:
            mark = {"queued": "·", "running": "▶", "done": "✓",
                    "failed": "✗", "cancelled": "—"}.get(t["status"], "?")
            print(f"  {mark} #{t['id']:<4} {t['status']:<9} {t['executor']:<12} "
                  f"{t['created_at_local']}  {t['goal'][:40]}")
        return 0

    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_tag(args, cfg) -> int:
    from . import tagging
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)

    if args.action == "run":
        if getattr(args, "pending", False):
            r = tagging.run_pending(
                con, cfg, llm.build(cfg, force_offline=args.offline),
                limit=args.limit if args.limit > 10 else 50)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
        r = tagging.run(con, cfg, llm.build(cfg, force_offline=args.offline),
                        limit=args.limit, dry=args.dry)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if args.action == "seed":
        n = tagging.seed(con)
        print(f"已写入 {n} 个起步标签" if n else "起步标签已存在")
        return 0
    if args.action == "rerun":
        if args.all:
            if not args.yes:
                raise SystemExit("清空全部派生标签需要 --yes 确认"
                                 "（只动派生层，原文不受影响）")
            n = tagging.forget_all(con)
            print(f"已清空 {n} 段。接下来跑 `pmem tag run --limit N` 分批重打。")
            return 0
        if not args.conversation:
            raise SystemExit("要么给 --conversation <id>，要么给 --all --yes")
        r = tagging.rerun_one(con, llm.build(cfg, force_offline=args.offline),
                              args.conversation)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if args.action == "propose":
        provider = llm.build(cfg, force_offline=args.offline)
        r = tagging.propose(con, cfg, provider,
                            limit=args.limit, batch_size=args.batch_size)
        cands = r["candidates"]
        if not args.no_merge and len(cands) >= 6:
            cands = tagging.consolidate(provider, cands)
        r["candidates"] = cands
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
        print(f"扫了 {r['scanned']} 条消息，{r['batches']} 批："
              f"原始候选 {r.get('raw_count', len(cands))} 个"
              f"{'' if args.no_merge else f'，合并后 {len(cands)} 个'}\n")
        cur_dim = None
        for c in cands[:args.top]:
            if c["dim"] != cur_dim:
                cur_dim = c["dim"]
                print(f"\n【{cur_dim}】")
            members = c.get("members") or []
            extra = "" if len(members) <= 1 else " ← " + "、".join(members[:3])
            parent = f"  ↑{c['parent']}" if c.get("parent") else ""
            print(f"  {c['count']:>3}  {c['name']:<10}{parent}{extra[:34]}")
        print("\n采纳：pmem tag add 名字 --dim 维度   （可一次给多个）")
        return 0
    if args.action == "add":
        for name in args.names:
            print(json.dumps(tagging.add_tag(con, name, dimension=args.dim,
                                             parent=args.parent,
                                             state="approved"), ensure_ascii=False))
        return 0
    if args.action in ("approve", "reject", "pending"):
        state = {"approve": "approved", "reject": "rejected", "pending": "pending"}[args.action]
        for tid in args.ids:
            print(json.dumps(tagging.set_state(con, tid, state), ensure_ascii=False))
        return 0
    # list
    rows = tagging.list_tags(con, state=args.state)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("还没有标签。先跑 `pmem tag seed` 或 `pmem tag run`。")
        return 0
    for t in rows:
        mark = {"approved": "✓", "pending": "?", "rejected": "✗"}.get(t["state"], "·")
        parent = f" ↑{t['parent']}" if t.get("parent") else ""
        print(f"  {mark} #{t['id']:<4} {t['state']:<9} {t['dimension']:<5} "
              f"{t['name']+parent:<16} 段 {t['uses']}")
    return 0


def cmd_verify(args, cfg) -> int:
    db_path = Path(args.db) if args.db else cfg.path("paths", "db")
    con = db.open_db(db_path, force=args.force)
    media = cfg.path("paths", "media")
    results: list[tuple[str, str, str]] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        results.append((name, "PASS" if ok else ("SKIP" if ok is None else "FAIL"), detail))

    # 1 索引存在且与消息表一致
    # 比的是**全部消息行**，不是 active 行：FTS5 是 external content 表，
    # 一行 messages 对应一行索引，purge 只把内容清空、不物理删行（见 README 的已知限制）。
    # 早先这里比 active，于是每 purge 一次这一项就假报警——它分不清"purge 过"和"索引漂移"。
    n_msg = con.execute("SELECT COUNT(*) FROM messages WHERE state='active'").fetchone()[0]
    n_all = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    n_idx = con.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    # purge 过的行不该留下任何可检索内容（漏清 content_indexed 才是真问题）
    leaked = con.execute(
        "SELECT COUNT(*) FROM messages WHERE state='purged' "
        "AND COALESCE(content_indexed, '') <> ''").fetchone()[0]
    add("FTS5 索引可用", n_idx == n_all and leaked == 0,
        f"active={n_msg} 全部={n_all} index={n_idx}"
        + (f" purged 残留={leaked}" if leaked else ""))

    # 2 中文精确子串命中
    if n_msg == 0:
        add("中文精确子串", None, "库是空的")
    else:
        row = con.execute(
            "SELECT id, content FROM messages WHERE state='active' "
            "AND length(content) >= 12 ORDER BY length(content) DESC LIMIT 50"
        ).fetchone()
        probe = None
        if row:
            c = row["content"]
            for i in range(len(c) - 3):
                seg = c[i:i + 3]
                if all(text.is_cjk(ch) for ch in seg):
                    probe = seg
                    break
        if probe:
            hits = search.search(con, cfg, probe, limit=5)
            add("中文精确子串", len(hits) > 0, f"探测词 {probe!r} 命中 {len(hits)} 条")
        else:
            add("中文精确子串", None, "没找到合适的中文探针")

    # 3 不存在的词必须返回 0（不编造）
    hits = search.search(con, cfg, "绝不存在的词组zzqx", limit=5)
    add("查不到就返回空", len(hits) == 0, f"命中 {len(hits)} 条（应为 0）")

    # 4 附件哈希一致
    rows = con.execute(
        "SELECT id, path, sha256 FROM attachments WHERE state='active' LIMIT 5"
    ).fetchall()
    if not rows:
        add("附件 sha256 一致", None, "没有附件")
    else:
        bad = 0
        for r in rows:
            p = media / r["path"]
            if not p.exists():
                bad += 1
                continue
            if hashlib.sha256(p.read_bytes()).hexdigest() != r["sha256"]:
                bad += 1
        add("附件 sha256 一致", bad == 0, f"抽查 {len(rows)} 个，异常 {bad} 个")

    # 5 导出往返
    work = db_path.parent / ".verify"
    work.mkdir(parents=True, exist_ok=True)
    try:
        p = work / "m.jsonl"
        n = export.export_jsonl(con, cfg, p)
        lines = sum(1 for _ in p.open(encoding="utf-8"))
        add("导出可往返", n == lines == n_msg, f"导出 {n} 行 / 库 {n_msg} 条")

        # 6 时间精度不撒谎
        fake = con.execute(
            "SELECT COUNT(*) FROM messages WHERE time_precision='unknown' AND created_at IS NOT NULL"
        ).fetchone()[0]
        missing = con.execute(
            "SELECT COUNT(*) FROM messages WHERE time_precision='message' AND created_at IS NULL"
        ).fetchone()[0]
        add("时间精度不撒谎", fake == 0 and missing == 0,
            f"unknown 却有时间的={fake}, message 却无时间的={missing}")

        # 6.5 证据门规则（纯函数，不调模型）
        v_fake = gate.validate({"sufficient": True, "used": [], "next": "answer"}, 3)
        v_ok = gate.validate({"sufficient": True, "used": [2], "next": "answer"}, 3)
        v_bad_ref = gate.validate({"sufficient": True, "used": [9], "next": "answer"}, 3)
        gate_ok = (v_fake.sufficient is False and v_ok.sufficient is True
                   and v_bad_ref.sufficient is False)
        add("证据门代码强制", gate_ok,
            "空 used 判不足 / 越界 ref 判不足 / 正常引证放行")

    # 7 purge 穿透（唯一的破坏性操作；必须在临时库上验，绝不碰真库）
        pdir = work / "purge"
        pmedia = pdir / "media"
        pmedia.mkdir(parents=True, exist_ok=True)
        pcon = db.open_db(pdir / "p.db", force=True)
        try:
            cid = store.upsert_conversation(
                pcon, source="live", source_id="purge-probe", title="purge 验收",
                started_at=clock.now_utc_iso(), time_precision="message")
            mid = store.add_message(
                pcon, conversation_id=cid, role="user", origin="user",
                content="purge 探针句：这一条必须被穿透清除",
                created_at=clock.now_utc_iso(), time_precision="message", source="live")
            store.store_attachment(
                pcon, pmedia, data=b"purge-probe-bytes", orig_name="p.txt",
                when_iso=clock.now_utc_iso(), message_id=mid, conversation_id=cid)
            before = search.search(pcon, cfg, "探针句", limit=5)
            apath = pcon.execute("SELECT path FROM attachments WHERE message_id = ?",
                                 (mid,)).fetchone()["path"]
            abspath = pmedia / apath
            purge.purge_message(pcon, pmedia, mid, hard_files=True)
            after = search.search(pcon, cfg, "探针句", limit=5)
            row = pcon.execute("SELECT state, content FROM messages WHERE id = ?",
                               (mid,)).fetchone()
            astate = pcon.execute("SELECT state FROM attachments WHERE message_id = ?",
                                  (mid,)).fetchone()["state"]
            logged = pcon.execute("SELECT COUNT(*) FROM purge_log WHERE target_id = ?",
                                  (mid,)).fetchone()[0]
            ok = (len(before) == 1 and len(after) == 0 and row["state"] == "purged"
                  and (row["content"] or "") == "" and astate == "purged"
                  and not abspath.exists() and logged == 1)
            add("purge 穿透四层", ok,
                f"索引 {len(before)}→{len(after)} 条｜消息={row['state']}｜附件={astate}"
                f"｜文件已删={not abspath.exists()}｜审计={logged}")
        finally:
            pcon.close()

    # 8 Git 安全闸门
        # 假仓库不能在 data/ 下（整个目录被 ignore），也不能放系统临时目录
        # （沙箱拒绝在那里建 .git）。放在项目根下的临时目录，用完删掉。
        # 探测文件用 .bin：`.gitignore` 里有 `*.db`，用 .db 会被判成"已忽略"，
        # 这条检查就永远"没拦截"了（实测踩过）。
        repo = Path(cfg.root) / ".pmem_verify_git"
        try:
            (repo / ".git").mkdir(parents=True, exist_ok=True)
            try:
                db.check_git_safety(repo / "probe.bin")
                add("Git 误提交防护", False, "没有拦截")
            except SystemExit:
                add("Git 误提交防护", True, "已拦截仓库内的数据库")
        finally:
            shutil.rmtree(repo, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    width = max(len(r[0]) for r in results)
    print(f"pmem {__version__} 自检   SQLite {sqlite3.sqlite_version}\n")
    ok_all = True
    for name, state, detail in results:
        if state == "FAIL":
            ok_all = False
        print(f"  [{state}] {name.ljust(width)}  {detail}")
    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pmem", description="本地个人记忆库（零向量检索）")
    p.add_argument("--db", help="数据库路径（默认取 config.toml）")
    p.add_argument("--force", action="store_true", help="跳过 Git 安全检查")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库").set_defaults(func=cmd_init)

    ip = sub.add_parser("import", help="导入外部对话")
    ip.add_argument("kind", choices=["takeout", "voyager"])
    ip.add_argument("source", help="zip 或 md 文件路径")
    ip.add_argument("--dry-run", action="store_true", help="只看清单，不写库")
    ip.add_argument("--limit", type=int, help="只处理前 N 场对话（调试用）")
    ip.add_argument("--skip-attachments", action="store_true")
    ip.add_argument("--include-video", action="store_true", help="连视频一起导入")
    ip.add_argument("--max-attachment-mb", type=float)
    ip.add_argument("--at", help="对话发生时间，如 2026-10-31T01:11:38+08:00")
    ip.add_argument("--date", help="只给日期，如 2026-10-26")
    ip.set_defaults(func=cmd_import)

    sp = sub.add_parser("search", help="检索记忆")
    sp.add_argument("query", nargs="?", default="")
    sp.add_argument("--tag", help="按已批准的标签检索")
    sp.add_argument("--dim", help="配合 --tag：限定维度（主题/地点/人物/机构/任务/情绪）")
    sp.add_argument("--exact", action="store_true",
                    help="配合 --tag：只精确匹配，不包含子级（默认包含）")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--conversation", type=int)
    sp.add_argument("--images", action="store_true",
                    help="按图片描述检索（需先跑 pmem describe）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    ds = sub.add_parser("describe", help="给图片写一句话描述（默认只处理少量，不扫全库）")
    ds.add_argument("--limit", type=int, default=5, help="处理几张（默认 5）")
    ds.add_argument("--id", help="只处理这些附件 id，逗号分隔")
    ds.add_argument("--offline", action="store_true")
    ds.set_defaults(func=cmd_describe)

    sl = sub.add_parser("sleep", help="手动标一个睡眠点（热记忆从这里重新开始）")
    sl.add_argument("--at", help="ISO 时间；默认现在")
    sl.add_argument("--note", help="备注")
    sl.add_argument("--list", action="store_true", help="列出手动标记过的睡眠点")
    sl.set_defaults(func=cmd_sleep)

    inf = sub.add_parser("infer", help="从原文推断（只写 derived，带证据，不覆盖已确认）")
    inf.add_argument("what", choices=["employment"], help="目前只支持任职经历")
    inf.add_argument("--limit", type=int, default=45, help="扫最近多少条含线索的消息")
    inf.add_argument("--dry", action="store_true", help="只显示推断结果，不写库")
    inf.add_argument("--offline", action="store_true")
    inf.set_defaults(func=cmd_infer)

    sh = sub.add_parser("show", help="看一条消息的全文")
    sh.add_argument("id", type=int)
    sh.set_defaults(func=cmd_show)

    hp = sub.add_parser("hot", help="看热记忆窗口")
    hp.add_argument("--hours", type=int, default=24)
    hp.set_defaults(func=cmd_hot)

    st = sub.add_parser("stats", help="统计")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stats)

    ep = sub.add_parser("export", help="导出为人类可读格式")
    ep.add_argument("--format", choices=["jsonl", "md", "both"], default="both")
    ep.add_argument("--out")
    ep.set_defaults(func=cmd_export)

    pp = sub.add_parser("purge", help="彻底清除（不可恢复）")
    pp.add_argument("--message", type=int)
    pp.add_argument("--conversation", type=int)
    pp.add_argument("--hard", action="store_true", help="同时删除附件文件")
    pp.add_argument("--note")
    pp.set_defaults(func=cmd_purge)

    sub.add_parser("verify", help="跑验收自检").set_defaults(func=cmd_verify)

    ap = sub.add_parser("ask", help="单次问答（走完整链路）")
    ap.add_argument("text")
    ap.add_argument("--offline", action="store_true", help="强制离线降级模式")
    ap.add_argument("--recall", action="store_true", help="强制检索历史")
    ap.add_argument("--json", action="store_true")
    ap.set_defaults(func=cmd_ask)

    cp = sub.add_parser("chat", help="交互式对话")
    cp.add_argument("--offline", action="store_true")
    cp.set_defaults(func=cmd_chat)

    gp = sub.add_parser("gaps", help="列出导入缺口")
    gp.add_argument("--status", default="open",
                    choices=["open", "filled", "ignored", "all"])
    gp.add_argument("--limit", type=int, default=300)
    gp.add_argument("--json", action="store_true")
    gp.set_defaults(func=cmd_gaps)

    atp = sub.add_parser("attach", help="手工补一个缺失附件")
    atp.add_argument("message_id", type=int)
    atp.add_argument("file")
    atp.set_defaults(func=cmd_attach)

    lgp = sub.add_parser("log", help="查看路由日志")
    lgp.add_argument("--last", type=int, default=20)
    lgp.add_argument("--json", action="store_true")
    lgp.set_defaults(func=cmd_log)

    tp = sub.add_parser("task", help="任务对象：登记、执行、收尾、查看")
    tsub = tp.add_subparsers(dest="action", required=True)

    t_start = tsub.add_parser("start", help="登记一个任务")
    t_start.add_argument("goal")
    t_start.add_argument("--executor", default="inline",
                         help="inline（pmem 自己）| external（脚本）| agent:<名字>")
    t_start.add_argument("--parent", type=int, help="从哪场对话发起的（会往里写一条可见消息）")
    t_start.add_argument("--status", default="running", choices=list(_TASK_STATUSES))
    t_start.set_defaults(func=cmd_task)

    t_fin = tsub.add_parser("finish", help="给任务收尾")
    t_fin.add_argument("task_id", type=int)
    t_fin.add_argument("--status", default="done", choices=["done", "failed", "cancelled"])
    t_fin.add_argument("--summary")
    t_fin.add_argument("--error")
    t_fin.set_defaults(func=cmd_task)

    t_log = tsub.add_parser("log", help="往任务的工作日志写一条（不进热窗）")
    t_log.add_argument("task_id", type=int)
    t_log.add_argument("text")
    t_log.set_defaults(func=cmd_task)

    t_ls = tsub.add_parser("list", help="列出任务")
    t_ls.add_argument("--status", default="all",
                      choices=["all", "queued", "running", "done", "failed", "cancelled"])
    t_ls.add_argument("--limit", type=int, default=20)
    t_ls.set_defaults(func=cmd_task)

    t_run = tsub.add_parser("run", help="用内置执行器跑一次（一次模型调用）")
    t_run.add_argument("task_id", type=int)
    t_run.add_argument("--dry", action="store_true", help="只打印要发的内容，不调用模型")
    t_run.add_argument("--offline", action="store_true")
    t_run.set_defaults(func=cmd_task)

    ctp = sub.add_parser("context", help="算出该带什么上下文（给运行时用，不生成、不写库）")
    ctp.add_argument("text")
    ctp.add_argument("--json", action="store_true")
    ctp.add_argument("--offline", action="store_true")
    ctp.add_argument("--source", help="来源（qq/telegram/wechat…）")
    ctp.add_argument("--session", help="外部会话 id：与 note 用同一个值才能对上")
    ctp.set_defaults(func=cmd_context)

    ntp = sub.add_parser("note", help="写一条消息（时间由 pmem 盖章）")
    ntp.add_argument("text")
    ntp.add_argument("--role", default="user", choices=["user", "assistant"])
    ntp.add_argument("--conversation", type=int)
    ntp.add_argument("--source", help="来源（qq/telegram/wechat…），配合 --session 用")
    ntp.add_argument("--session", help="外部会话 id：同一 id 稳定落到同一场 pmem 会话")
    ntp.add_argument("--attach", action="append", metavar="路径",
                     help="附上图片/文件（可重复给多次）：IM 收到的附件走这里")
    ntp.add_argument("--describe", action="store_true",
                     help="收图时顺便写一句话描述（每张一次视觉调用；默认不写）")
    ntp.add_argument("--offline", action="store_true")
    ntp.set_defaults(func=cmd_note)

    en = sub.add_parser("entity", help="实体层：人物/机构 + 归属区间（只写派生层）")
    ensub = en.add_subparsers(dest="action", required=True)
    ensub.add_parser("sync", help="把已批准的 人物/机构 标签投影成实体").set_defaults(
        func=cmd_entity)
    en_ls = ensub.add_parser("list", help="列出实体")
    en_ls.add_argument("--kind", choices=["person", "org"])
    en_ls.add_argument("--roles", action="store_true", help="只看角色实体（老板/猎头…）")
    en_ls.add_argument("--limit", type=int, default=40)
    en_ls.set_defaults(func=cmd_entity)
    en_show = ensub.add_parser("show", help="看一个实体：提及次数与归属区间")
    en_show.add_argument("name", help="实体名或 id")
    en_show.set_defaults(func=cmd_entity)
    en_ten = ensub.add_parser("tenure", help="登记任职区间（日期不知道就留空）")
    en_ten.add_argument("person")
    en_ten.add_argument("--org", help="机构名（可留空）")
    en_ten.add_argument("--from", dest="valid_from", help="起始 ISO 时间")
    en_ten.add_argument("--to", dest="valid_to", help="结束 ISO 时间")
    en_ten.add_argument("--origin", default="derived", choices=["derived", "confirmed"])
    en_ten.set_defaults(func=cmd_entity)
    en_cand = ensub.add_parser("candidates", help="某时刻的候选人（只给候选，不下结论）")
    en_cand.add_argument("--when", help="ISO 时间；省略=不限")
    en_cand.add_argument("--org")
    en_cand.add_argument("--roles", action="store_true", help="只看角色实体")
    en_cand.set_defaults(func=cmd_entity)
    en_td = ensub.add_parser("tenure-del", help="删掉一条区间（清掉推断错的）")
    en_td.add_argument("id", type=int)
    en_td.set_defaults(func=cmd_entity)
    en_tc = ensub.add_parser("tenure-confirm", help="把推断的区间标成用户确认")
    en_tc.add_argument("id", type=int)
    en_tc.set_defaults(func=cmd_entity)
    en_un = ensub.add_parser("unresolved", help="需要人确认的角色实体")
    en_un.add_argument("--limit", type=int, default=20)
    en_un.set_defaults(func=cmd_entity)
    en_ask = ensub.add_parser("ask", help="把需要确认的角色实体挂进待裁决清单（不催）")
    en_ask.add_argument("--limit", type=int, default=20)
    en_ask.set_defaults(func=cmd_entity)

    pf = sub.add_parser("profile", help="用户状态：断言+有效期+指向原文的证据")
    pfsub = pf.add_subparsers(dest="action", required=True)
    pf_add = pfsub.add_parser("add", help="记一条状态（推断必须带证据）")
    pf_add.add_argument("kind", choices=list(("就业", "居住", "身份", "偏好", "关系", "其他")))
    pf_add.add_argument("subject")
    pf_add.add_argument("value")
    pf_add.add_argument("--from", dest="valid_from")
    pf_add.add_argument("--to", dest="valid_to")
    pf_add.add_argument("--evidence", default="", help="逗号分隔的消息 id（指向原文）")
    pf_add.add_argument("--origin", default="derived", choices=["derived", "confirmed"])
    pf_add.set_defaults(func=cmd_profile)
    pf_ls = pfsub.add_parser("list", help="列出当前有效的状态")
    pf_ls.add_argument("--subject")
    pf_ls.add_argument("--kind")
    pf_ls.add_argument("--all", action="store_true", help="连被取代的旧值一起看")
    pf_ls.add_argument("--limit", type=int, default=50)
    pf_ls.set_defaults(func=cmd_profile)
    pf_cf = pfsub.add_parser("confirm", help="把一条推断升级为用户确认")
    pf_cf.add_argument("id", type=int)
    pf_cf.set_defaults(func=cmd_profile)
    pf_hs = pfsub.add_parser("history", help="一个断言的全部版本（什么时候变的）")
    pf_hs.add_argument("kind")
    pf_hs.add_argument("subject")
    pf_hs.set_defaults(func=cmd_profile)
    pf_ex = pfsub.add_parser("export", help="导出成人读的 Markdown（只读表，不反向写库）")
    pf_ex.add_argument("--out", default=None, help="输出目录（默认取 config 的 paths.profile）")
    pf_ex.set_defaults(func=cmd_profile)

    rv = sub.add_parser("review", help="复查：每周找矛盾 / 每月重整理后对比（只记录，不改记忆）")
    rvsub = rv.add_subparsers(dest="action", required=True)
    rv_w = rvsub.add_parser("weekly", help="最近 N 天 vs 更早：找前后矛盾的说法")
    rv_w.add_argument("--days", type=int, default=7)
    rv_w.add_argument("--limit", type=int, default=25)
    rv_w.add_argument("--offline", action="store_true")
    rv_w.set_defaults(func=cmd_review)
    rv_m = rvsub.add_parser("monthly", help="影子库里重打一遍，与现有标签对比（漂移）")
    rv_m.add_argument("--limit", type=int, default=50, help="重整理几场会话")
    rv_m.add_argument("--offline", action="store_true")
    rv_m.set_defaults(func=cmd_review)

    pd = sub.add_parser("pending", help="待裁决清单：看、逐条回答、关闭（不催促）")
    pdsub = pd.add_subparsers(dest="action", required=True)
    pd_ls = pdsub.add_parser("list", help="列出待裁决项")
    pd_ls.add_argument("--status", default="open",
                       choices=["open", "proposed", "applied", "rejected",
                                "resolved", "dismissed"])
    pd_ls.add_argument("--limit", type=int, default=50)
    pd_ls.set_defaults(func=cmd_pending)
    pd_ans = pdsub.add_parser("answer", help="记录我的说明（不改状态）")
    pd_ans.add_argument("id", type=int)
    pd_ans.add_argument("text")
    pd_ans.set_defaults(func=cmd_pending)
    pd_pr = pdsub.add_parser("propose", help="让系统提一条具体动作（只提议，不动数据）")
    pd_pr.add_argument("id", type=int)
    pd_pr.add_argument("--offline", action="store_true")
    pd_pr.set_defaults(func=cmd_pending)
    pd_ap = pdsub.add_parser("apply", help="执行已提议的动作（写审计，失败回滚）")
    pd_ap.add_argument("id", type=int)
    pd_ap.set_defaults(func=cmd_pending)
    pd_rj = pdsub.add_parser("reject", help="驳回已提议的动作")
    pd_rj.add_argument("id", type=int)
    pd_rj.add_argument("--reason", default=None)
    pd_rj.set_defaults(func=cmd_pending)
    pd_lg = pdsub.add_parser("log", help="看执行审计（谁在什么时候执行了什么）")
    pd_lg.add_argument("--limit", type=int, default=20)
    pd_lg.set_defaults(func=cmd_pending)
    for name, helptext in (("resolve", "关闭这条（已处理）"),
                           ("dismiss", "关掉这条（不是问题）"),
                           ("reopen", "重新打开")):
        p3 = pdsub.add_parser(name, help=helptext)
        p3.add_argument("id", type=int)
        p3.set_defaults(func=cmd_pending)

    dy = sub.add_parser(
        "daily",
        help="闲时整理：转冷的会话切段+打标（正常会自动跑，这条给定时任务/补跑用）")
    dy.add_argument("--limit", type=int, default=None, help="这次最多整理几场（默认取配置 2）")
    dy.add_argument("--now", action="store_true", help="忽略间隔限制，立刻跑")
    dy.add_argument("--dry", action="store_true", help="只看队列，不动")
    dy.add_argument("--offline", action="store_true")
    dy.set_defaults(func=cmd_daily)

    cv = sub.add_parser("conv", help="会话开关：设为默认不调用（除非当前这句话点到触发词）")
    cvsub = cv.add_subparsers(dest="action", required=True)
    cv_mute = cvsub.add_parser("mute", help="设为默认不调用")
    cv_mute.add_argument("id", help="会话 id / source_id / 对话链接")
    cv_mute.add_argument("--triggers", default="",
                         help="逗号分隔的触发词，例：差分机,科幻小说")
    cv_mute.set_defaults(func=cmd_conv)
    cv_un = cvsub.add_parser("unmute", help="恢复调用")
    cv_un.add_argument("id")
    cv_un.set_defaults(func=cmd_conv)
    cvsub.add_parser("list", help="列出默认不调用的会话").set_defaults(func=cmd_conv)

    tg = sub.add_parser("tag", help="打标：闭集词表 + 待审队列")
    tgsub = tg.add_subparsers(dest="action", required=True)

    tg_run = tgsub.add_parser("run", help="按会话打标：切话题段 + 多维标签")
    tg_run.add_argument("--limit", type=int, default=10, help="处理几场会话")
    tg_run.add_argument("--pending", action="store_true",
                        help="只处理「待整理」队列（睡了一觉之后转冷的那些），闲时跑")
    tg_run.add_argument("--dry", action="store_true")
    tg_run.add_argument("--offline", action="store_true")
    tg_run.set_defaults(func=cmd_tag)

    tg_rr = tgsub.add_parser("rerun", help="重打：提示词改了之后用（先丢旧段再重打）")
    tg_rr.add_argument("--conversation", type=int, help="只重打这一场")
    tg_rr.add_argument("--all", action="store_true", help="清空全部派生标签（配合 tag run 重打）")
    tg_rr.add_argument("--yes", action="store_true", help="--all 的确认开关")
    tg_rr.add_argument("--offline", action="store_true")
    tg_rr.set_defaults(func=cmd_tag)

    tgsub.add_parser("seed", help="写入起步词表").set_defaults(func=cmd_tag)

    tg_pr = tgsub.add_parser("propose", help="只提议不打标：从真实消息里归纳候选类别")
    tg_pr.add_argument("--limit", type=int, default=200)
    tg_pr.add_argument("--batch-size", type=int, default=20)
    tg_pr.add_argument("--top", type=int, default=40)
    tg_pr.add_argument("--no-merge", action="store_true", help="不做合并，看原始候选")
    tg_pr.add_argument("--json", action="store_true")
    tg_pr.add_argument("--offline", action="store_true")
    tg_pr.set_defaults(func=cmd_tag)

    tg_add = tgsub.add_parser("add", help="手工加词（直接进正式词表）")
    tg_add.add_argument("names", nargs="+")
    tg_add.add_argument("--dim", default="主题", choices=list(_DIMENSIONS))
    tg_add.add_argument("--parent", help="父级名字（is-a）")
    tg_add.set_defaults(func=cmd_tag)

    tg_ls = tgsub.add_parser("list", help="列出标签")
    tg_ls.add_argument("--state", default="all",
                       choices=["all", "approved", "pending", "rejected"])
    tg_ls.add_argument("--json", action="store_true")
    tg_ls.set_defaults(func=cmd_tag)

    for name, help_text in (("approve", "批准（转为正式词表，立刻可检索）"),
                            ("reject", "拒绝"),
                            ("pending", "打回待审")):
        p2 = tgsub.add_parser(name, help=help_text)
        p2.add_argument("ids", type=int, nargs="+")
        p2.set_defaults(func=cmd_tag, action=name)

    return p


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    cfg = load()
    try:
        return int(args.func(args, cfg) or 0)
    except ValueError as e:
        print(f"参数有问题：{e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
