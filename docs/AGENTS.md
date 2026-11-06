# AGENTS —— 给写代码的 AI 读的入口

> 这份文件是**给 agent 看的**，不是给人看的营销页。人看 [`../README.md`](../README.md)；
> 为什么这么设计看 [`DESIGN.md`](DESIGN.md)；现在到哪一步看 [`HANDOVER.md`](HANDOVER.md)。
>
> 目标：一个 agent 读完这份 + 跑一遍 `run_all`，就能在**不破坏不变量**的前提下改代码。

---

## 0. 三十秒建立基线

```bat
python tools\run_all.py          :: 96 项上限；空库时 94 通过 + 2 跳过
```

**先跑，再动手。** 这是你判断"我改坏了没有"的唯一基线。跑完记住那两个数字：
`通过数` 与 `上限`。

---

## 1. 铁律（改任何东西之前先看）

| # | 铁律 | 违反的后果 |
|---|---|---|
| 1 | **禁止向量检索**。不引 embedding、不做语义相似度、不做混合召回 | 项目存在的理由没了 |
| 2 | **原文只追加**。`messages.content` 永不改写；派生层可重建 | 追溯性一次性销毁 |
| 3 | **时间戳只能由存储层盖章**。`note()` 不接受调用方传时间 | 时间轴会倒挂、会撒谎 |
| 4 | **`unknown` 的时间必须是 `NULL`**。宁可没有，也不填占位 | 用错误数据掩盖缺失数据 |
| 5 | **`derived` 永不覆盖 `confirmed`**。推断冲突时两条并存 | 用户的确认被机器悄悄推翻 |
| 6 | **测试必须用临时库**；任何脚本不许写真实库（`data/memory.db`） | 污染生产数据、毒化统计 |
| 7 | **零第三方依赖**（只用标准库） | 破坏"clone 下来就能跑" |

第 6 条有原型事故：`tools/smoke_http.py` 曾经直接写真实库，每跑一次就留下 6 条测试消息
+ 3 条路由日志。现在它先用 `con.backup()` 复制成 `data/_http_smoke.db` 再跑。

---

## 2. 代码地图（改之前先找到地方）

```
pmem/
  __init__.py    __version__ 与 SCHEMA_VERSION（版本只看这里）
  db.py          DDL / _MIGRATIONS / open_db / check_git_safety
  store.py       add_message / store_attachment（sha256 内容寻址）
  text.py        tokenize（汉字间插空格）/ build_match（phrase 查询）
  search.py      search / search_multi / search_by_tag / hot_window / segment_start_iso
  route.py       decide（规则下限 + 第三档复核）/ RECALL_HINTS
  gate.py        validate（三条代码规则）
  chat.py        ChatSession.respond（完整链路）/ _add
  tagging.py     seed / apply_segments / mark_cold_before / run_pending / rerun_one
  daily.py       due / run / maybe_auto（转冷队列的自动消化）
  actions.py     propose / apply / reject / KINDS（两段式动作，闭集）
  entities.py    ensure_entity / add_tenure / candidates（只给候选）
  profile.py     add_fact / confirm / export_md
  review.py      add_pending / list_pending / answer / weekly / monthly
  images.py      describe_batch / search / render
  infer.py       employment（只写 derived）
  ports.py       build_context / note（**对外唯一两个出口**）
  prompt.py      SYSTEM_* / render_hot / render_hits / build_chat_messages / load_persona
  config.py      DEFAULTS（新增配置项从这儿加）
  clock.py       UTC 存储 / 本地展示 / decay
  cli.py         build_parser + cmd_*（所有命令）
  export.py gaps.py purge.py tasks.py llm.py importers/
tools/
  run_all.py       验收总入口 + EXPECTED（断言条数校验）
  smoke_*.py       各层行为断言（详见 run_all 的 EXPECTED）
  eval_router.py   A/B 对照（调真实模型，不在 run_all 里）
  check_privacy.py 隐私扫描（结构性规则 + 可选私有词表）
  load_samples.py  合成样例载入器
  mock_llm.py      假模型服务（smoke_http 用）
```

---

## 3. 数据模型速查

| 表 | 关键列 | 说明 |
|---|---|---|
| `conversations` | `source`, `source_id`, `no_recall`, `recall_triggers` | 同 `source_id` 合并；`no_recall=1` 默认不调用 |
| `messages` | `seq`, `role`, `content`, `content_indexed`, `created_at`, `time_precision`, `state` | raw 层；`state='purged'` 是唯一"删除" |
| `attachments` | `sha256`, `path`, `description` | 内容寻址；`description` 是唯一描述来源 |
| `segments` / `segment_tags` | `start_seq..end_seq` | 标签挂在段上，不挂消息上 |
| `tags` | `dimension`, `parent_id`, `path`, `state` | 六维闭集；`state='pending'` 不参与检索 |
| `cold_queue` | `conversation_id`, `done_at` | 转冷待整理（幂等） |
| `pending_resolutions` | `sig`, `answer`, `proposal`, `status` | 两段式；`sig` 去重（**不要用 payload LIKE**） |
| `entity_tenure` | `entity_id`, `org_id`, `valid_from/to`, `basis`, `origin` | 区间未知留 NULL |
| `profile_facts` | `kind`, `subject`, `value`, `evidence`, `origin`, `superseded_by` | 旧值不删 |
| `action_log` | `kind`, `payload`, `result`, `actor` | `apply` 的审计 |

状态机：

```
pending:  open --answer--> (answered，隐含在 answer 列)
                       --propose--> proposed --apply--> applied
                                          --reject-> rejected
冷队列:   入队(幂等) --run_pending 成功--> done_at 非空；失败留队列
```

---

## 4. 命令契约（对外只有两个出口）

```bat
:: 全局参数（--db / --force）必须写在子命令**前面**
run.cmd --db data/sample.db search 咖啡

:: 取上下文：返回 JSON，含 messages / need_recall / keywords / retrieved / gate_*
run.cmd context "<用户这句话>" --json --source wechat --session <key>

:: 写消息：时间由 pmem 盖；--attach 可多次；--describe 顺手写图片描述
run.cmd note "<一条消息>" --role user|assistant --source wechat --session <key>
```

- 同一 `(source, session)` 稳定落进同一场会话（跨天续聊不断开）。
- `context` **不写消息**（但会 upsert 会话行，别把它当严格只读）。
- `note` 写完 `role=assistant` 那条时，会顺手触发 `daily.maybe_auto()` —— 队列空时只是一次 SQL。

---

## 5. 验收机制（改完必须全跑）

```bat
python tools\run_all.py            :: 全跑
python tools\run_all.py smoke_tag  :: 只跑名字含 smoke_tag 的
python tools\run_all.py --list     :: 看清单与期望条数
```

- 清单由 `tools/run_all.py` 的 `EXPECTED` 决定，**不是** `tools/smoke_*.py` 通配
  （`smoke_http.py` 是手动联调脚本，无断言，混进来会变成假绿灯）。
- 每项都校验 **断言条数**：少一条、多一条、无输出都算失败。
- `verify` 的期望是区间 `(7, 9)`：空库时有 2 项跳过（无探针词 / 无附件）。
- **加断言时必须同步改 `EXPECTED`**，否则 `run_all` 会报 COUNT。

加断言的套路：在对应 `tools/smoke_*.py` 里造数据 → 调被测函数 → 收集
`(名字, 是否通过, 细节)` → 最后统一打印。断言要写**行为**，并尽量带**反向对照**。

---

## 6. 修改流程

1. `python tools\run_all.py` —— 记录基线。
2. 改代码。跨层改动优先怀疑 `search.py` / `ports.py` / `prompt.py`。
3. `python tools\run_all.py` —— 必须回到全绿；条数变了就同步 `EXPECTED`。
4. 更新文档：
   - 改了行为 → `docs/DESIGN.md` 对应小节（它按「结论/理由/被否决/怎么验收」四段写）；
   - 改了状态 → `docs/HANDOVER.md`（含验收条数）；
   - 改了对外契约 → `docs/IM_INTEGRATION.md`。
5. `git commit`：一条提交讲一件改动，说明"为什么"，不只是"改了什么"。

---

## 7. 常见任务配方

| 任务 | 步骤 |
|---|---|
| **加一个 CLI 命令** | `cli.py` 里 `sub.add_parser(...)` + `cmd_xxx()` + 在 `build_parser` 注册 `set_defaults(func=cmd_xxx)`；加一条断言；更新 README 命令表 |
| **加一种动作**（两段式） | `actions.KINDS` 加名字 → `validate` 加校验 → `_exec_xxx` 实现 → 注册进 `_EXEC` → 加断言（提议不动数据 / 执行成功 / 状态机拒绝） |
| **改提示词** | 只动 `prompt.py` 的 `SYSTEM_*` 或 `build_*`；改完跑 `smoke_tag`（它验标签接入）与 `smoke_ports`（验上下文形状）。提示词改动会改变模型行为，**不要顺手改断言去迁就** |
| **加配置项** | `config.DEFAULTS` 加默认值（记得写注释）→ 调用处 `cfg.get("段","键", default=...)` → 文档补一行 |
| **改 schema** | 在 `db.py` 的 `DDL` 里加表/列，**同时**往 `_MIGRATIONS` 加 `(表, 列定义)`（老库靠它迁移）→ `SCHEMA_VERSION += 1` |
| **加样例数据** | 只改 `samples/sample_data.json`；不要在里面写真实人名/地点/公司 |

---

## 8. 坑（都踩过，别重复）

- **Windows 中文终端是 GBK**：`cli._setup_console()` 在输出被重定向时切 UTF-8；
  `run.cmd` 必须保持**纯 ASCII**（cmd.exe 按 OEM 代码页读 .bat）。
- **不要用 PowerShell 的 `Set-Content -Encoding UTF8` 改 UTF-8 源文件**：会读成 GBK 再写回，中文全毁。
  用编辑工具的写入接口，或 Python 以 `encoding="utf-8", newline="\n"` 写。
- **trigram 对中文无效**（两字词 0 命中）；中文靠 `text.tokenize()` 预分词 + phrase 查询。
- **FTS5 是 external content 表**：一行 messages 对应一行索引；`purge` 只清空内容不删行，
  所以校验索引一致性要跟**全部消息行**比，不是 active 行。
- **取完再过滤是错的**：排除当前会话 / 静音会话 / 热窗段都必须下推到 SQL，否则名额被占满。
- **时间比较注意 naive/aware**：`clock.parse_iso` 统一成带 tz 的 UTC。
- **`sig` 去重不能用 `payload LIKE`**：JSON 里的引号会被转义，匹配不上。
- **`--db` 是全局参数**，写在子命令前面；写后面会被 argparse 拒掉。
- **离线 provider 不能用来打标**：标签是空的，却会把会话标成"已整理"，等于永久丢掉打标机会。

---

## 9. 不可以做的事

- 引入任何第三方依赖（含 `numpy`、`pydantic`、`requests`）。
- 加向量检索、语义相似度、embedding 字段。
- 把 `tools/smoke_http.py` 一类"无断言的手动脚本"塞进 `run_all`。
- 在测试里写真实库路径；把个人数据（真实地名/公司/人名）写进代码、注释或样例。
- 为了让测试变绿去删断言 —— `EXPECTED` 的条数校验就是为这个准备的。
- 直接改 `messages.content` 来"修"数据（要用 `purge`，它会写审计）。

---

## 10. 三份文档的分工

| 文件 | 读者 | 回答什么 |
|---|---|---|
| `README.md` | 人（第一次来） | 这是什么、凭什么信、怎么跑起来、不做什么 |
| `docs/AGENTS.md`（本文） | agent（要改代码） | 规矩在哪、代码在哪、改完怎么验、哪里会踩坑 |
| `docs/DESIGN.md` | 人（要理解取舍） | 每条决策的结论/理由/否决项/验收 |
| `docs/HANDOVER.md` | 接手的 agent 或人 | 现在到哪一步、下一步做什么 |

改代码前，先读第 1 节；改完，回到第 5 节。
