"""载入合成样例数据，让你 clone 下来就能看到东西。

为什么需要它：`verify` 在空库上只会有 2 项 SKIP，`search` 什么都搜不到。
一个"能跑但什么都演示不了"的仓库，别人看不出它好在哪。

用法：

    python tools/load_samples.py                  # 建 data/sample.db
    python tools/load_samples.py --force          # 覆盖已有的那个
    python tools/load_samples.py --db data/x.db   # 指定库

载入之后可以这样玩：

    run.cmd --db data/sample.db stats
    run.cmd --db data/sample.db search 咖啡
    run.cmd --db data/sample.db search --tag 咖啡
    run.cmd --db data/sample.db hot
    run.cmd --db data/sample.db review weekly --offline

数据本身在 `samples/sample_data.json`，全部是虚构的：没有真人、真公司、真行程。
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.version_info < (3, 11):
    print(f"需要 Python 3.11+（当前是 {sys.version.split()[0]}）：tomllib 在 3.11 才有。")
    print(f"换一个解释器再跑，例如：  py -3.12 {Path(__file__).name}")
    raise SystemExit(2)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pmem import clock, db, entities, review, store, tagging  # noqa: E402
from pmem.config import load  # noqa: E402

SAMPLE_JSON = ROOT / "samples" / "sample_data.json"


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """生成一个纯色 PNG（不引第三方库）。样例图不需要好看，只需要是真图片。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _when(iso_minute: str, offset_min: int = 0) -> str:
    """把本地时间串当成 UTC+8 的时刻，转成库里的 UTC ISO。"""
    dt = clock.parse_iso(iso_minute + ":00+08:00")
    if dt is None:
        raise SystemExit(f"日期格式不对：{iso_minute}")
    from datetime import timedelta
    return clock.to_iso_utc(dt + timedelta(seconds=offset_min))


def main(argv: list[str]) -> int:
    cfg = load()
    # 默认写到 data/sample.db，**绝不碰 data/memory.db**——
    # 样例库和真实记忆是两回事，混在一起既危险又难清理。
    db_path = cfg.path("paths", "db").parent / "sample.db"
    force = "--force" in argv
    for i, a in enumerate(argv):
        if a == "--db" and i + 1 < len(argv):
            db_path = Path(argv[i + 1])

    if db_path.exists() and not force:
        print(f"{db_path} 已存在。要覆盖请加 --force（或换个 --db 路径）。")
        return 1
    for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        p.unlink(missing_ok=True)

    data = json.loads(SAMPLE_JSON.read_text(encoding="utf-8"))
    con = db.open_db(db_path, force=True)
    media = cfg.path("paths", "media")

    stat = {"conversations": 0, "messages": 0, "segments": 0, "tags": 0,
            "attachments": 0, "pending": 0, "tenure": 0}

    # 1) 词表（先建父级，再建子级——层级要在入库时就写对）
    for t in data.get("标签", []):
        r = tagging.add_tag(con, t["名字"], dimension=t["维度"],
                            parent=t.get("父级"), state="approved")
        if r.get("ok"):
            stat["tags"] += 1

    # 2) 对话与消息
    conv_ids: dict[str, int] = {}
    for c in data["对话"]:
        start = _when(c["日期"])
        cid = store.upsert_conversation(
            con, source="sample", source_id=c["id"], title=c["标题"],
            started_at=start, ended_at=_when(c["日期"], 60 * (len(c["消息"]) - 1)),
            time_precision="message")
        conv_ids[c["id"]] = cid
        stat["conversations"] += 1
        for i, (role, text) in enumerate(c["消息"], 1):
            store.add_message(
                con, conversation_id=cid, role=role, origin=role, content=text,
                created_at=_when(c["日期"], 60 * (i - 1)),
                time_precision="message", source="sample")
            stat["messages"] += 1

    # 3) 两张图（一张有描述、一张没有——正好演示"未描述时模型看不到"）
    png_a = _png(64, 64, (210, 180, 140))
    png_b = _png(64, 64, (120, 160, 200))
    first_msg = con.execute(
        "SELECT id FROM messages WHERE conversation_id = ? ORDER BY seq LIMIT 1",
        (conv_ids["s01"],)).fetchone()["id"]
    aid1, _rel1, _ = store.store_attachment(
        con, media, data=png_a, orig_name="装备清单.png",
        when_iso=clock.now_utc_iso(), message_id=first_msg, conversation_id=conv_ids["s01"])
    aid2, _rel2, _ = store.store_attachment(
        con, media, data=png_b, orig_name="路线草图.png",
        when_iso=clock.now_utc_iso(), message_id=first_msg, conversation_id=conv_ids["s01"])
    con.execute("UPDATE attachments SET description = ? WHERE id = ?",
                ("一张手写的徒步装备清单，圈出了雨具和头灯", aid1))
    stat["attachments"] = 2
    _ = aid2  # 故意不写描述：演示"未描述"分支

    # 4) 话题段 + 标签关联
    for s in data.get("话题段", []):
        cid = conv_ids[s["对话"]]
        msgs = tagging._conversation_messages(con, cid)
        r = tagging.apply_segments(con, cid, msgs, {"segments": [{
            "from": s["从"], "to": s["到"], "title": s["标题"], "reason": "（样例）",
            "tags": [{"dim": "主题", "name": n} if not n.startswith(("甲", "乙"))
                     else {"dim": "地点", "name": n} for n in s["标签"]],
        }]})
        stat["segments"] += int(r.get("segments") or 0)

    # 5) 一条待裁决（演示 review / pending 两段式）
    for p in data.get("待裁决", []):
        nid = review.add_pending(con, p["类型"], {
            "sig_key": f"entity:sample-{p['话题']}",
            "topic": p["话题"], "old": p["较早"], "new": p["较近"], "why": p["为什么"]})
        if nid:
            stat["pending"] += 1

    # 6) 一条归属区间（derived，等你确认——演示 entity tenure-confirm）
    for t in data.get("任职区间", []):
        entities.add_tenure(con, t["人"], t["机构"], valid_from=t["起"],
                            valid_to=t["止"], origin="derived", basis=t["依据"])
        stat["tenure"] += 1

    con.close()
    print(f"样例库已生成：{db_path}")
    print(f"  {stat['conversations']} 场对话 / {stat['messages']} 条消息 / "
          f"{stat['segments']} 个话题段 / {stat['tags']} 个标签 / "
          f"{stat['attachments']} 张图 / {stat['pending']} 条待裁决 / "
          f"{stat['tenure']} 条任职区间")
    print("\n试试：")
    print(f"  run.cmd --db {db_path} stats")
    print(f"  run.cmd --db {db_path} search 咖啡")
    print(f"  run.cmd --db {db_path} search --tag 咖啡")
    print(f"  run.cmd --db {db_path} hot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
