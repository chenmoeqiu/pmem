"""Google Takeout 适配器（主数据源）。

为什么它是主源：
- 每条 prompt 都有服务端时间戳（毫秒级 UTC）
- 100% 的记录带对话 URL，可与 Voyager 精确 JOIN
- 附带你在 Gemini 里上传的**原始附件**（比导出工具重编码的副本更保真）

它不提供助手回复；助手侧由 voyager 适配器补充。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Iterator

from .. import clock, gaps, store
from ..config import Config

PROMPT_PREFIX = "Prompted "


def _pick_activity_entry(z: zipfile.ZipFile) -> zipfile.ZipInfo:
    cands = [
        i for i in z.infolist()
        if i.filename.lower().endswith(".json") and "gemini" in i.filename.lower()
    ]
    if not cands:
        cands = [
            i for i in z.infolist()
            if i.filename.lower().endswith(".json")
            and ("活动" in i.filename or "myactivity" in i.filename.lower())
        ]
    if not cands:
        raise SystemExit("没有在压缩包里找到 Gemini 活动记录 JSON。")
    return max(cands, key=lambda i: i.file_size)


def _url_of(rec: dict[str, Any]) -> str | None:
    for d in (rec.get("details") or []):
        u = d.get("url", "") or ""
        if "gemini.google.com/app/" in u:
            return u
    return None


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def iter_prompts(records: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for rec in records:
        title = rec.get("title", "") or ""
        if not title.startswith(PROMPT_PREFIX):
            continue
        url = _url_of(rec)
        if not url:
            continue
        yield {
            "url": url,
            "slug": _slug(url),
            "text": title[len(PROMPT_PREFIX):],
            "time": rec.get("time"),
            "attachments": rec.get("attachedFiles") or [],
            "ref": rec.get("time"),
        }


def group_conversations(prompts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in prompts:
        groups.setdefault(p["url"], []).append(p)
    for url in groups:
        groups[url].sort(key=lambda p: p["time"] or "")
    return groups


def import_takeout(
    con,
    cfg: Config,
    zip_path: Path,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    skip_attachments: bool = False,
    skip_video: bool | None = None,
    max_attachment_mb: float | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    skip_video = (
        cfg.get("import", "skip_video", default=True) if skip_video is None else skip_video
    )
    max_mb = (
        cfg.get("import", "max_attachment_mb", default=20)
        if max_attachment_mb is None else max_attachment_mb
    )
    media_root = cfg.path("paths", "media")
    off = cfg.get("runtime", "utc_offset_hours", default=8)

    z = zipfile.ZipFile(zip_path)
    entry = _pick_activity_entry(z)
    folder = entry.filename.rsplit("/", 1)[0] + "/"
    if verbose:
        print(f"活动记录: {entry.filename}  ({entry.file_size / 1048576:.1f} MB)")
    records = json.loads(z.read(entry).decode("utf-8-sig"))

    all_prompts = list(iter_prompts(records))
    skipped_records = len(records) - len(all_prompts)
    groups = group_conversations(all_prompts)

    items = sorted(groups.items(), key=lambda kv: kv[1][0]["time"] or "")
    if limit:
        items = items[:limit]

    if dry_run:
        print(f"\n{'日期范围':<28}{'轮':>4}{'附件':>5}  首条内容")
        print("-" * 100)
        for url, prompts in items:
            start = clock.local_str(prompts[0]["time"], off)
            end = clock.local_str(prompts[-1]["time"], off)
            span = start[:16] if start == end else f"{start[:10]}~{end[:10]}"
            n_att = sum(len(p["attachments"]) for p in prompts)
            head = prompts[0]["text"].replace("\n", " ")[:44]
            print(f"{span:<28}{len(prompts):>4}{n_att:>5}  {head}")
        print("-" * 100)
        print(f"共 {len(items)} 场对话 / {sum(len(p) for _, p in items)} 条 prompt / "
              f"{len(all_prompts)} 条可导入（另有 {skipped_records} 条 UI 操作记录被跳过）")
        print("这是 dry-run，没有写数据库。")
        return {"dry_run": True, "conversations": len(items), "prompts": len(all_prompts)}

    stat = {
        "conversations": 0, "messages": 0, "attachments": 0,
        "attachment_bytes": 0, "skipped_large": 0, "skipped_video": 0,
        "missing_in_zip": 0, "skipped_records": skipped_records, "gaps": 0,
    }
    sizes = {i.filename: i.file_size for i in z.infolist()}

    for idx, (url, prompts) in enumerate(items, 1):
        conv_id = store.upsert_conversation(
            con,
            source="takeout",
            source_id=_slug(url),
            url=url,
            title=prompts[0]["text"].replace("\n", " ")[:60],
            started_at=prompts[0]["time"],
            ended_at=prompts[-1]["time"],
            time_precision="message",
        )
        stat["conversations"] += 1

        for seq, p in enumerate(prompts, 1):
            msg_id = store.add_message(
                con,
                conversation_id=conv_id,
                seq=seq,
                role="user",
                origin="user",
                content=p["text"],
                created_at=p["time"],
                time_precision="message",
                source="takeout",
                source_ref=p["ref"],
            )
            stat["messages"] += 1

            if skip_attachments:
                continue
            for name in p["attachments"]:
                full = folder + name
                if full not in sizes:
                    stat["missing_in_zip"] += 1
                    gaps.record(con, conversation_id=conv_id, message_id=msg_id,
                                source="takeout", orig_name=name,
                                reason="not_in_archive")
                    stat["gaps"] += 1
                    continue
                size = sizes[full]
                if skip_video and store.kind_of(name) == "video":
                    stat["skipped_video"] += 1
                    gaps.record(con, conversation_id=conv_id, message_id=msg_id,
                                source="takeout", orig_name=name,
                                reason="video_skipped")
                    stat["gaps"] += 1
                    continue
                if size > max_mb * 1048576:
                    stat["skipped_large"] += 1
                    gaps.record(con, conversation_id=conv_id, message_id=msg_id,
                                source="takeout", orig_name=name,
                                reason="too_large")
                    stat["gaps"] += 1
                    continue
                data = z.read(full)
                _aid, _path, inserted = store.store_attachment(
                    con, media_root,
                    data=data, orig_name=name, when_iso=p["time"],
                    message_id=msg_id, conversation_id=conv_id,
                    utc_offset_hours=off,
                )
                if inserted:
                    stat["attachments"] += 1
                    stat["attachment_bytes"] += size

        if verbose and idx % 50 == 0:
            print(f"  ... {idx}/{len(items)} 场，{stat['messages']} 条消息，"
                  f"{stat['attachments']} 个附件")

    if verbose:
        print(f"\n导入完成：{stat['conversations']} 场对话 / {stat['messages']} 条消息 / "
              f"{stat['attachments']} 个附件（{stat['attachment_bytes']/1048576:.1f} MB）")
        if stat["skipped_video"]:
            print(f"  跳过视频 {stat['skipped_video']} 个（可在 config.toml 打开）")
        if stat["skipped_large"]:
            print(f"  跳过超大附件 {stat['skipped_large']} 个")
        if stat["missing_in_zip"]:
            print(f"  压缩包中缺失附件 {stat['missing_in_zip']} 个")
        if stat["gaps"]:
            print(f"  已记录 {stat['gaps']} 条导入缺口（pmem gaps 查看，pmem attach 手工补）")
    return stat
