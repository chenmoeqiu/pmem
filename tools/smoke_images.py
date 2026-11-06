"""图片描述验收。不调 API（用假 provider 给描述）。

要守的边界：
1. 描述落库、可被 `pmem search --images` 查到
2. **离线不编造**：没有视觉能力时返回空串，不写假描述
3. 单张失败不中断批量（文件不存在那种）
4. 只处理你点的那几张，不扫全库
5. 描述不会替代原文：附件行、sha256、文件都还在

用法：python tools/smoke_images.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pmem import clock, db, images, store  # noqa: E402
from pmem.config import load  # noqa: E402


class FakeVision:
    name = "stub"

    def describe_image(self, data: bytes, mime: str, hint: str = ""):
        return ("白板照片：上面写着 TGV 深径比 50:90 和两行工艺参数",
                {"model": "stub", "tokens_in": 0, "tokens_out": 0})

    def tag_batch(self, *a, **k):
        return "{}", {}


class OfflineVision:
    name = "offline"

    def describe_image(self, data: bytes, mime: str, hint: str = ""):
        return "", {}


def main() -> int:
    cfg = load()
    workdir = cfg.path("paths", "db").parent
    media = cfg.path("paths", "media")
    tmp = workdir / "_images_smoke.db"
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)
    con = db.open_db(tmp, force=True)

    cid = store.upsert_conversation(con, source="live", source_id="img-smoke",
                                    title="图片验收", started_at=clock.now_utc_iso(),
                                    time_precision="message")
    mid = store.add_message(con, conversation_id=cid, role="user", origin="user",
                            content="看这张白板", created_at=clock.now_utc_iso(),
                            time_precision="message", source="live")
    store.store_attachment(con, media, data=b"fake-png-bytes-1", orig_name="a.png",
                           when_iso=clock.now_utc_iso(), message_id=mid,
                           conversation_id=cid)
    store.store_attachment(con, media, data=b"fake-png-bytes-2", orig_name="b.png",
                           when_iso=clock.now_utc_iso(), message_id=mid,
                           conversation_id=cid)
    # 第三个：文件先删掉，用来验"单张失败不中断"
    aid3, rel3, _ = store.store_attachment(
        con, media, data=b"fake-png-bytes-3", orig_name="c.png",
        when_iso=clock.now_utc_iso(), message_id=mid, conversation_id=cid)
    (Path(media) / rel3).unlink(missing_ok=True)

    results: list[tuple[str, bool, str]] = []

    # 1. 只处理指定的那一张
    first = con.execute("SELECT id FROM attachments ORDER BY id LIMIT 1").fetchone()["id"]
    r1 = images.describe_batch(con, cfg, FakeVision(), ids=[first])
    results.append(("只处理指定的图", r1["described"] == 1 and r1["tried"] == 1,
                    f"tried={r1['tried']} described={r1['described']}"))

    # 2. 描述能查到
    hits = images.search(con, "白板", limit=5)
    results.append(("描述可检索", len(hits) >= 1, f"命中 {len(hits)} 张"))

    # 3. 批量时单张文件缺失不中断
    r2 = images.describe_batch(con, cfg, FakeVision(), limit=5)
    results.append(("单张失败不中断批量",
                    r2["failed"] >= 1 and r2["described"] >= 1,
                    f"described={r2['described']} failed={r2['failed']}"))

    # 4. 离线不编造
    before = con.execute(
        "SELECT COUNT(*) FROM attachments WHERE description IS NOT NULL").fetchone()[0]
    con.execute("UPDATE attachments SET description = NULL WHERE id = ?", (first,))
    r3 = images.describe_batch(con, cfg, OfflineVision(), ids=[first])
    after = con.execute(
        "SELECT COUNT(*) FROM attachments WHERE description IS NOT NULL").fetchone()[0]
    results.append(("离线不写假描述", r3["described"] == 0 and r3["skipped"] >= 1,
                    f"described={r3['described']} skipped={r3['skipped']}"))

    # 5. 描述不替代原文：附件行、sha256、文件都在
    row = con.execute("SELECT sha256, path, state FROM attachments WHERE id = ?",
                      (first,)).fetchone()
    keep = (row["sha256"] and (Path(media) / row["path"]).exists()
            and row["state"] == "active")
    results.append(("描述不替代原文", bool(keep),
                    f"sha256 保留={bool(row['sha256'])} 文件在={ (Path(media)/row['path']).exists() }"))

    # 6. 渲染成"相关图片"块
    txt = images.render(images.search(con, "白板", limit=3))
    results.append(("能渲染成相关图片块", "相关" not in txt and "白板" in txt,
                    f"{len(txt)} 字"))

    # 7. 热窗必须让模型知道"这条带了图"——否则它会对着"看这张"三个字凭空作答
    from pmem import prompt as _prompt, search as _search
    con.execute("UPDATE attachments SET description = NULL WHERE message_id = ?", (mid,))
    hot = _search.hot_window(con, cfg, since=clock.window_start_iso(6))
    unread = _prompt.render_hot(hot, 0)
    con.execute("UPDATE attachments SET description = '某型号激光器面板' WHERE id = ?",
                (first,))
    hot2 = _search.hot_window(con, cfg, since=clock.window_start_iso(6))
    read = _prompt.render_hot(hot2, 0)
    ok_img = ("附图 3 张，未描述" in unread
              and "看不到" in unread
              and "附图 3 张，已描述" in read)
    results.append(("热窗告诉模型有没有图", bool(ok_img),
                    f"未描述={'未描述' in unread} 已描述={'已描述：' in read}"))

    con.close()
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)

    width = max(len(r[0]) for r in results)
    ok_all = True
    print("图片描述\n")
    for name, ok, detail in results:
        if not ok:
            ok_all = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
