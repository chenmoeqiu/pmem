"""数据库层：连接、PRAGMA、建表、Git 安全检查。

设计约束：
- 所有表用 STRICT（SQLite >= 3.37），防止把字符串塞进整数列。
- 原始对话 append-only；state='purged' 是唯一的破坏性出口。
- FTS5 用 external content 表，避免原文在索引里再存一份。
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import SCHEMA_VERSION

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

-- 一场对话。source_id 是跨数据源的全局键（Gemini 的 app id / URL slug）。
CREATE TABLE IF NOT EXISTS conversations (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,              -- takeout | voyager | live
    source_id      TEXT NOT NULL,              -- Gemini app id
    url            TEXT,
    title          TEXT,
    started_at     TEXT,                       -- ISO-8601 UTC
    ended_at       TEXT,
    time_precision TEXT NOT NULL DEFAULT 'unknown',   -- message | conversation | unknown
    state          TEXT NOT NULL DEFAULT 'active',    -- active | purged
    created_at     TEXT NOT NULL,
    UNIQUE (source_id)
) STRICT;

-- 一条消息。content 是原文（人类可读、可迁移）；content_indexed 是预分词结果。
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,          -- 会话内顺序 1..n
    role            TEXT NOT NULL,             -- user | assistant | system
    origin          TEXT NOT NULL,             -- user | assistant  可信度来源
    content         TEXT NOT NULL,
    content_indexed TEXT NOT NULL,
    created_at      TEXT,                      -- ISO-8601 UTC；未知则 NULL
    time_precision  TEXT NOT NULL,             -- message | conversation | unknown
    source          TEXT NOT NULL,             -- takeout | voyager | live
    source_ref      TEXT,                      -- 原始记录标识，便于回溯
    state           TEXT NOT NULL DEFAULT 'active',
    UNIQUE (conversation_id, seq)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_msg_conv  ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_msg_time  ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_msg_state ON messages(state);

-- 附件：图片/文档/视频。文件按 sha256 内容寻址存放在 data/media 下。
CREATE TABLE IF NOT EXISTS attachments (
    id              INTEGER PRIMARY KEY,
    message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,             -- image | document | video | other
    orig_name       TEXT,
    sha256          TEXT NOT NULL,
    path            TEXT NOT NULL,             -- 相对 media 根的路径
    bytes           INTEGER,
    mime            TEXT,
    created_at      TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'active'
) STRICT;

CREATE INDEX IF NOT EXISTS idx_att_msg  ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_att_sha  ON attachments(sha256);

-- 标签词表（闭集）。派生数据，可由原文重建。
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL DEFAULT 'topic',  -- topic | emotion | entity
    state      TEXT NOT NULL DEFAULT 'approved', -- approved | pending
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS tag_aliases (
    alias  TEXT PRIMARY KEY,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE
) STRICT;

CREATE TABLE IF NOT EXISTS memory_tags (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    weight     REAL NOT NULL DEFAULT 1.0,
    source     TEXT NOT NULL DEFAULT 'agent',
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, tag_id)
) STRICT;

-- 人在回路：待你裁决的记忆冲突
CREATE TABLE IF NOT EXISTS pending_resolutions (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                 -- contradiction | duplicate | new_tag
    payload     TEXT NOT NULL,                 -- JSON
    status      TEXT NOT NULL DEFAULT 'open',  -- open | resolved | dismissed
    created_at  TEXT NOT NULL,
    resolved_at TEXT
) STRICT;

-- purge 的审计：只记 id 与时间，绝不留内容
CREATE TABLE IF NOT EXISTS purge_log (
    id          INTEGER PRIMARY KEY,
    target_kind TEXT NOT NULL,                 -- message | conversation
    target_id   INTEGER NOT NULL,
    purged_at   TEXT NOT NULL,
    note        TEXT
) STRICT;

-- 两段式的执行审计（v15）：propose 只写 pending_resolutions.proposal，不碰任何记忆；
-- apply 才改数据，并且必须在这里留一行——谁在什么时候把哪个动作执行了。
CREATE TABLE IF NOT EXISTS action_log (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER,                        -- 来源待裁决 id（手动执行时为空）
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,                  -- 执行时的动作 JSON
    result     TEXT NOT NULL,                  -- 执行结果 JSON
    actor      TEXT,                           -- cli / im / 其他调用方
    created_at TEXT NOT NULL
) STRICT;

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content_indexed,
    content='messages',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content_indexed)
    VALUES (new.id, new.content_indexed);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content_indexed)
    VALUES ('delete', old.id, old.content_indexed);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content_indexed)
    VALUES ('delete', old.id, old.content_indexed);
    INSERT INTO messages_fts(rowid, content_indexed)
    VALUES (new.id, new.content_indexed);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS tags_fts USING fts5(
    name,
    content='tags',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS tags_ai AFTER INSERT ON tags BEGIN
    INSERT INTO tags_fts(rowid, name) VALUES (new.id, new.name);
END;

CREATE TRIGGER IF NOT EXISTS tags_ad AFTER DELETE ON tags BEGIN
    INSERT INTO tags_fts(tags_fts, rowid, name) VALUES ('delete', old.id, old.name);
END;

-- 路由决策日志：回答"它为什么没想起来"的唯一依据
CREATE TABLE IF NOT EXISTS routing_log (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER,
    input_preview   TEXT NOT NULL,
    domain          TEXT NOT NULL,             -- work | casual
    need_recall     INTEGER NOT NULL,          -- 0 | 1
    keywords        TEXT,                      -- JSON 数组
    ambiguous       INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL,             -- rule | model | offline
    reason          TEXT,
    hits            INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER,
    model           TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    created_at      TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_route_time ON routing_log(created_at);

-- 导入缺口：有引用、包内无实体。承认自己丢了什么，比悄悄吞掉好。
CREATE TABLE IF NOT EXISTS import_gaps (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    message_id      INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    orig_name       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open | filled | ignored
    filled_attachment_id INTEGER,
    created_at      TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_gaps_status ON import_gaps(status);

-- 任务对象：只记录"存在/状态/产出/归属"，不执行任何东西。
-- 执行器是谁由 executor 字段说明，pmem 不关心也不需要知道。
CREATE TABLE IF NOT EXISTS tasks (
    id                     INTEGER PRIMARY KEY,
    goal                   TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'queued',
        -- queued | running | done | failed | cancelled  （queued 现在就留着，
        -- 将来别人要加队列时不用改表）
    executor               TEXT NOT NULL DEFAULT 'inline',
        -- inline（pmem 自己一次调用）| external（任何脚本）| agent:<名字>
    conversation_id        INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
        -- 这个任务自己的工作日志线程（source='task'），不进热窗
    parent_conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    source_message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    result_summary         TEXT,
    error                  TEXT,
    started_at             TEXT,
    finished_at            TEXT,
    created_at             TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- 话题段：会话内按话题切出来的区间。**原文不复制**，只记 start_seq..end_seq。
-- 一个会话可以有多个段；一条消息也可以属于多个段（话题交织时）。
CREATE TABLE IF NOT EXISTS segments (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    start_seq       INTEGER NOT NULL,
    end_seq         INTEGER NOT NULL,
    title           TEXT,
    reason          TEXT,               -- 为什么从这里切（可审计）
    state           TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_seg_conv ON segments(conversation_id, start_seq);

-- 标签挂在"段"上，不挂在消息上：标签描述的是"这一段在讲什么"。
CREATE TABLE IF NOT EXISTS segment_tags (
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    weight     REAL NOT NULL DEFAULT 1.0,
    source     TEXT NOT NULL DEFAULT 'agent',
    created_at TEXT NOT NULL,
    PRIMARY KEY (segment_id, tag_id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_segtag_tag ON segment_tags(tag_id);

-- 实体层（v10）：人物/机构是"实体"，标签只是它的一个视角。
-- 为什么需要它：标签能把"老板"挂在段上，但回答不了"半年前那个老板是不是昨天那个"。
-- 实体 + 归属区间（tenure）+ 提及（mention）才能把这个问题摆到人面前。
CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,                 -- person | org
    name       TEXT NOT NULL,                 -- 显示名；角色实体就叫"老板"
    is_role    INTEGER NOT NULL DEFAULT 0,    -- 1 = 角色（老板/猎头），0 = 具名
    aliases    TEXT,                          -- JSON 数组
    created_at TEXT NOT NULL,
    UNIQUE(kind, name)
) STRICT;

CREATE TABLE IF NOT EXISTS entity_mentions (
    id          INTEGER PRIMARY KEY,
    segment_id  INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    UNIQUE(segment_id, entity_id)
) STRICT;

-- 归属区间：某人在某机构任职的时间段。有效期未知就留 NULL，不假装知道。
CREATE TABLE IF NOT EXISTS entity_tenure (
    id         INTEGER PRIMARY KEY,
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    org_id     INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    valid_from TEXT,
    valid_to   TEXT,
    evidence   TEXT,                          -- JSON：来自哪几条消息（指向原文）
    origin     TEXT NOT NULL DEFAULT 'derived',  -- derived | confirmed
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_mention_entity ON entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_tenure_entity  ON entity_tenure(entity_id);

-- 手动睡眠点（v13）：作息不规律时，自己标一个"这里睡了一觉"。
-- 优先于自动推断——人被机器误判时，得有个手动出口。
CREATE TABLE IF NOT EXISTS sleep_marks (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,               -- 边界时刻（ISO，UTC）
    note       TEXT,
    created_at TEXT NOT NULL
) STRICT;

-- 用户状态（v11）：断言 + 有效期 + **指向原文的证据** + 确认方式。
-- 它不是摘要：每条都能点回原话；旧值不删（superseded_by 串起来）。
-- origin=derived 的推断**永远不能覆盖** origin=confirmed 的确认值。
CREATE TABLE IF NOT EXISTS profile_facts (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,               -- 就业/居住/身份/偏好/关系/其他
    subject       TEXT NOT NULL,               -- 关于谁/什么（实体名或自由文本）
    value         TEXT NOT NULL,               -- 断言内容（不是转述摘要）
    valid_from    TEXT,                        -- 有效期起；未知留 NULL
    valid_to      TEXT,                        -- 止；NULL = 至今
    evidence      TEXT NOT NULL DEFAULT '[]',  -- JSON：消息 id 列表 → 原文
    origin        TEXT NOT NULL DEFAULT 'derived',  -- derived | confirmed
    superseded_by INTEGER,                     -- 被哪条取代；**旧条不删**
    created_at    TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_fact_subject ON profile_facts(kind, subject);

-- 待整理队列（v7）：热记忆按"清醒段"滚动，睡了一觉之后上一段就转冷。
-- 转冷的那一刻入队，真正的切段+打标留到闲时（API 谷价时段）批量做。
-- 只放**还没打过标**的会话：已整理过的不会再排一次，避免重复烧调用。
CREATE TABLE IF NOT EXISTS cold_queue (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    marked_at       TEXT NOT NULL,
    done_at         TEXT,
    UNIQUE(conversation_id)
) STRICT;

-- 证据门的结果（第三批加入；老库用 _migrate 补列）
-- gate_sufficient: NULL=未启用证据门, 0=判不足, 1=判充足
"""

# 增量迁移：向已存在的 routing_log 补列（SQLite 的 ADD COLUMN 很便宜）
_MIGRATIONS: list[tuple[str, str]] = [
    ("routing_log", "gate_sufficient INTEGER"),
    ("routing_log", "gate_used TEXT"),
    ("routing_log", "gate_missing TEXT"),
    ("routing_log", "tokens_gate_in INTEGER"),
    ("conversations", "task_id INTEGER"),
    ("messages", "tagged_at TEXT"),
    # 多维 + 层级的标签（v6）：dimension 是闭集，parent_id 是 is-a 的父节点
    ("tags", "dimension TEXT NOT NULL DEFAULT 'topic'"),
    ("tags", "parent_id INTEGER"),
    ("tags", "path TEXT"),
    # v7：默认不调用的会话（例如"自己的科幻创作草稿"，不想让它在检索里冒出来）。
    # 只在派生层/查询层生效，原文一个字不动。
    ("conversations", "no_recall INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "recall_triggers TEXT"),
    # v14：区间要能带上"原话依据"，人才好逐条确认（光有消息 id 不够直观）
    ("entity_tenure", "basis TEXT"),
    # v12：图片描述（让图片在记忆里存在）。只存一句话描述，不是原文替代。
    ("attachments", "description TEXT"),
    ("attachments", "described_at TEXT"),
    # v9：复查的人工回答（只记录，改状态要显式 resolve）
    ("pending_resolutions", "answer TEXT"),
    ("pending_resolutions", "answered_at TEXT"),
    # 去重用：同一件事只记一次，否则每周复查会把同一条重复报上来
    ("pending_resolutions", "sig TEXT"),
    # v15：两段式——propose 只写 proposal，apply 才执行并写 action_log
    ("pending_resolutions", "proposal TEXT"),
    ("pending_resolutions", "proposed_at TEXT"),
    ("pending_resolutions", "applied_at TEXT"),
    ("pending_resolutions", "apply_result TEXT"),
    ("pending_resolutions", "actor TEXT"),
]


def _migrate(con: sqlite3.Connection) -> None:
    for table, column_def in _MIGRATIONS:
        col = column_def.split()[0]
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")

    # v5 的扁平标签（dimension='topic' 之类）在 v6 的多维模型下无效，
    # 清掉它们——打标是派生数据，重跑即可。级联会同时清掉关联行。
    valid = ("主题", "地点", "人物", "机构", "任务", "情绪")
    marks = ",".join("?" * len(valid))
    con.execute(f"DELETE FROM tags WHERE dimension NOT IN ({marks})", valid)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def init_schema(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    _migrate(con)
    con.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )


def _find_git_root(p: Path) -> Path | None:
    cur = p.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return None


_GIT_CANDIDATES = (
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files\Git\bin\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
)


def _git_exe() -> str | None:
    """找 git 可执行文件。

    不能只靠 PATH：Git for Windows 默认不把 bin 加进某些进程的环境变量，
    实测装好之后从新开的子进程里仍然 `FileNotFoundError`，于是安全检查会
    误判成"无法确认是否被忽略"而**拒绝启动**（踩过）。
    """
    found = shutil.which("git")
    if found:
        return found
    for cand in _GIT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def check_git_safety(db_path: Path, force: bool = False) -> str | None:
    """数据库落在 Git 仓库内且未被忽略时，拒绝启动。

    个人记忆库被误提交进公开仓库是这类项目最典型、最不可挽回的事故：
    Git 历史删不掉。
    """
    root = _find_git_root(db_path)
    if root is None:
        return None

    ignored = None
    git = _git_exe()
    if git is not None:
        try:
            r = subprocess.run(
                [git, "check-ignore", "-q", str(db_path.resolve())],
                cwd=str(root), capture_output=True, timeout=10,
            )
            ignored = (r.returncode == 0)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            ignored = None

    if ignored is True:
        return None
    if ignored is None:
        msg = (f"警告：数据库位于 Git 仓库 {root} 内，且无法确认是否被忽略。\n"
               f"  文件: {db_path}\n  请确认 data/ 已写入 .gitignore。")
        if force:
            print(msg)
            return None
        raise SystemExit(msg + "\n  （确认安全后可加 --force 跳过）")

    raise SystemExit(
        f"拒绝启动：数据库文件在 Git 仓库内且未被 .gitignore 忽略\n"
        f"  仓库: {root}\n  文件: {db_path}\n"
        f"个人记忆库绝不能被提交，Git 历史无法撤销。\n"
        f"  请把 data/ 加入 .gitignore；确认安全后可加 --force 跳过。"
    )


def open_db(db_path: Path, force: bool = False) -> sqlite3.Connection:
    check_git_safety(db_path, force=force)
    con = connect(db_path)
    init_schema(con)
    return con
