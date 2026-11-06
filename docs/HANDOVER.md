# HANDOVER —— 当前状态与下一步

> **给新会话（人或 AI）看的。** 读完这份 + 跑一遍验收，就能接上工作，
> 不需要翻之前的对话记录。深度设计理由见 [`DESIGN.md`](DESIGN.md)。
>
> 分工：本文件答「现在到哪一步」，`DESIGN.md` 答「为什么是这个设计」。
>
> 最后更新：2026-11-06 · schema v15 · 验收 96 项全过

## 1. 这是什么

`pmem` 是一个**本地个人记忆库**：所有对话以带时间戳的纯文本存在本地 SQLite 里，
检索靠**标签**和**原文精确匹配**，**禁止向量检索**。

目标不是"更懂你"，是**不再乱联想**——没有指名的东西不会被唤醒。

## 2. 三十秒验证它没坏

```bat
python tools\run_all.py        :: 96 项（空库时 94 + 2 项 SKIP），一条命令
```

它会跑 `verify` + 8 个 smoke 套件，并且**校验每项的断言条数**——断言被删掉时，
只看"全部通过"是看不出来的（那正是假绿灯）。所以要单独跑某一项时，用它：

```bat
python tools\run_all.py smoke_tag    :: 只跑名字含 smoke_tag 的
run.cmd verify                       :: 只跑 verify 的 9 项
```

`tools\smoke_http.py` 不在里面：它是**手动**联调脚本，要先在 8791 起假模型服务，
输出是给人看的叙述、没断言，服务没起时退出码是 -1。

## 3. 一页架构

```
导入（Takeout / Voyager）→ 原文层（append-only + 时间戳 + sha256 附件）
                              ↓
                     打标：话题段 + 多维标签（闭集词表 + 待审队列）
                              ↓
        ┌──────────── 检索 ────────────┐
     标签（主路径）              原文 FTS（兜底：型号/编号/人名）
        └────────────┬────────────────┘
                     ↓
    路由（四支 + 第三档复核）→ 证据门（够不够）→ 生成
                     ↓
        热记忆（当前「清醒段」整段注入）／冷记忆（其余，可检索）
                     ↑
              两个出口一个入口：context / note
```

运行时（DSH、IM 机器人）**只调 `context` 和 `note`**，不自己拼上下文、不自己存历史。

## 4. 已经做完的（含验收位置）

| 能力 | 状态 | 验收 |
|---|---|---|
| 存储 / 导入 / 导出 / purge（四层穿透） | ✅ | `verify` 第 5/8 项 |
| 中文精确检索（预分词，不用 trigram） | ✅ | `verify` 第 2/3 项 |
| 标签检索（含 is-a 查父含子） | ✅ | `smoke_tag` 第 6/12 项 |
| **标签接入对话链路**（标签主、全文兜底） | ✅ | `smoke_tag` 第 13 项 |
| 路由四支 + 第三档复核 | ✅ | `eval_router.py`（A 组 1.00） |
| 证据门（三条规则代码强制） | ✅ | `verify` 第 7 项 |
| 证据不足时**先反问细节** | ✅ | `smoke_tag` 第 18 项 |
| 记忆分层：睡眠窗口边界 + 整段注入 | ✅ | `smoke_tag` 第 10 项 |
| **不重不漏**：滑出即可检索 | ✅ | `smoke_tag` 第 11/12 项 |
| 转冷队列（睡眠边界触发） | ✅ | `smoke_tag` 第 14–16 项 |
| 静音会话（默认不调用 + 触发词放行） | ✅ | `smoke_tag` 第 8/9 项 |
| 打标（多维 + 话题段 + 闭集 + 待审） | ✅ | `smoke_tag` 第 1–7 项 |
| 任务对象（执行器中立） | ✅ | `smoke_task` 8 项 |
| `context` / `note` 跨进程 | ✅ | `smoke_ports` 16 项 |
| 复查：周矛盾 / 月漂移 / 待裁决（不催改） | ✅ | `smoke_review` 12 项 |
| 实体层（人物/机构 + 归属区间 + 候选） | ✅ | `smoke_entity` 8 项 |
| 用户状态（断言+有效期+证据，derived/confirmed） | ✅ | `smoke_profile` 7 项 |
| 图片描述（一句话，可检索，进上下文） | ✅ | `smoke_images` 7 项 |
| **热窗标注"带图/未描述"**（模型知道有图，看不到就不猜） | ✅ | `smoke_images` 第 7 项 |
| 自动推断（任职经历，只写 derived） | ✅ | `smoke_infer` 6 项 |
| **转冷后自动整理**（闲时触发，幂等） | ✅ | `smoke_tag` 第 17–19 项 |
| **两段式动作确认**（propose 不动数据 → apply 才执行 + 审计） | ✅ | `smoke_review` 第 7–12 项 |
| **人设/语气可配**（`persona.md`，不覆盖记忆规则） | ✅ | `smoke_task`（人设进系统提示词、可关） |

## 5. 验收清单（当前 96 项）

```bat
python tools\run_all.py             :: 96（空库 94 + 2 SKIP；含断言条数校验）
```

它就是下面这 10 条的汇总，所以单独调试时也可以逐条跑：

```bat
run.cmd verify                      :: 9（空库 7 + 2 SKIP）
python tools\smoke_task.py          ::  8
python tools\smoke_tag.py           :: 22
python tools\smoke_ports.py         :: 16
python tools\smoke_review.py        :: 12
python tools\smoke_entity.py        ::  8
python tools\smoke_profile.py       ::  7
python tools\smoke_images.py        ::  7
python tools\smoke_infer.py         ::  6
python tools\check_privacy.py       ::  1
```

**改完必须全跑。** 这些不是"代码覆盖率"，是**行为断言**（一段能自动判定真假的检查）：不重不漏、派生不覆盖确认、
反向对照、词汇鸿沟、睡眠窗口边界。

## 6. 明确没做的（按优先级）

| 优先级 | 缺什么 | 说明 |
|---|---|---|
| 高 | 真运行时跑量 | IM 链路已通（微信实测），但没跑满 20 轮真实对话 |
| 低 | 动作种类只有 6 类（见 `pmem/actions.py` 的 KINDS） | 遇到没覆盖的动作时再加，不预先设计 |
| 中 | 排查抽取质量 | 任职推断会混淆"被拒的 offer"与"任职"，日期存在误推 |
| 低 | C 类指代追问（"某司"） | 不回也不太影响 |
| 低 | 记忆曲线 / 情绪权重 | 暂用时间衰减（半衰期 180 天）代替；**权重钩子已留**（`search(..., weight=)`），只差算法 |
| 低 | 增量导入 | 现在要手动导 Takeout |

## 7. 下一步（具体到命令）

1. **跑满 20 轮真实 IM 对话**，然后看路由记录：
   ```bat
   run.cmd log --last 20
   ```
   关注：误查（闲聊却去检索）、漏查（该查却没查）。

2. **给 `tools\run_all.py` 加 CI**（GitHub Actions，Windows + Linux）——脚本已就绪，缺的只是 workflow。

3. **处理待裁决队列**（用户决定，不催）：
   ```bat
   run.cmd pending list
   ```

## 8. 不可动的四条红线

改任何东西之前先看这四条——它们不是规范，是项目存在的理由：

1. **禁止向量检索**（不是"暂时不用"）
2. **原文全量保存**，不用摘要替代
3. **每条消息必须有时间戳**，精度诚实标注（`unknown` 必须是 NULL）
4. **时间戳由存储层盖章**，调用方无权传入

## 9. 几个容易踩的关键约定

- **注入边界 = 排除边界**：热记忆和冷检索排除读同一个 `segment_start_iso()`，
  否则会出现"既没注入又查不到"的空洞。
- **睡眠边界**：间隔 ≥ `sleep_gap_hours`（6）**且**与 `sleep_window`（默认 01:00-08:00）
  重叠 ≥ 2 小时。不是"跨自然日"——熬夜场景会把昨天带进今天。可用 `pmem sleep` 手动纠正。
- **转冷即整理，且整理自己发生**：边界之前未打标的会话自动进 `cold_queue`；
  一轮对话结束（`chat._add` / `ports.note` 写完助手那条）顺手消化最多 `cold.limit` 场，
  两次之间至少隔 `cold.interval_hours`（默认 6）。人工补跑：`pmem daily --now`。
  队列空时只是一次 SQL——**不要把它挪进 `context`**（那是每轮都要读的路径，而且 context 只该算上下文）。
- **derived 永远不覆盖 confirmed**：推断、自动打标都只写 `derived`；用户确认过的不动，冲突时两条并存。
- **派生层可重建**：表结构改动走 `_MIGRATIONS`（加列几乎免费），旧数据不用迁移。
- **`purge` 是唯一破坏性操作**，穿透原文/FTS/派生层/附件，且写审计。
- **两段式**：`pending propose` 只写 `proposal` 一列，**不动任何记忆**；`apply` 才改，
  改之前再校验一次（提议之后数据可能变了），整条包在事务里失败即回滚，成功写 `action_log`。
- **人设**：`persona.md`（已 gitignore）里的文字会拼在系统提示词**最前面**；
  `config.toml` 的 `[chat] persona = "..."` 可以一句话覆盖。人设只改语气，
  **不能改记忆规则**（没有记录就说没有、引用过去要带时间）——提示词里明写了这条边界。

## 10. 目录

```
pmem\            核心库（零第三方依赖，29 个文件 / 约 7100 行，含 importers 子包）
  __init__.py    版本号与 SCHEMA_VERSION（看版本只看这里）
  db.py          建表 + 增量迁移 + Git 安全防护
  store.py       原文与附件（sha256 内容寻址）
  text.py        中文预分词检索
  search.py      双索引 + 热窗 + 睡眠边界 + 不重不漏
  route.py       意图路由（规则下限 + 第三档复核）
  gate.py        证据门
  tagging.py     切段 + 多维标签 + 闭集词表 + 转冷队列
  entities.py    实体 + 归属区间（只给候选，不下结论）
  profile.py     用户状态（断言 + 有效期 + 证据，可导出 md）
  images.py      图片描述
  infer.py       从原文推断任职经历（只写 derived）
  review.py      周复查 / 月对比 / 待裁决
  actions.py     两段式动作（propose 只写建议 / apply 才执行 + 写 action_log）
  daily.py       闲时整理：转冷队列的自动消化（一轮结束顺手跑几场）
  ports.py       对外两个出口：context / note
  tasks.py       任务对象（只记录、不执行，执行器中立）
  chat.py        完整对话链路（context → 模型 → note）
  prompt.py      提示词与上下文渲染
  llm.py         模型调用（api / offline 两条路径）
  config.py      配置默认值（config.toml 覆盖）
  clock.py       UTC 存储 / 本地展示 / 时间衰减
  export.py      导出（人类可读）
  gaps.py        导入缺口
  purge.py       唯一破坏性操作
  cli.py         命令行
  __main__.py    python -m pmem 入口
tools\           验收与评估（14 个 .py）
  run_all.py      一条命令跑完 96 项（并校验断言条数，见第 2 节）
  smoke_task/tag/ports/review/entity/profile/images/infer.py   8 个验收套件，共 86 项
  eval_router.py   路由对照实验（A 独立路由 / B agent 自主）
  eval_emotion.py  情绪规则 A/B 对照（可复现）
  smoke_http.py    模型 HTTP 层的手动联调（跑在库的**副本**上，不在 run_all 里）
  mock_llm.py      假 provider（让断言不依赖网络）
  tag_curate.py    词表确定性归并 → 生成词表提案与批量入库脚本
docs\
  DESIGN.md      设计理由（为什么这么做、否决了什么）
  HANDOVER.md    本文件（现在到哪一步）
  AGENTS.md      给改代码的 AI 读的入口（规矩/地图/验收/坑）
  RELEASE.md     发布清单（能给别人看了吗、还差什么）
  OWNER.md       留给作者自己写的三页（现在是空的）
  IM_INTEGRATION.md  IM 接入运行手册
```

根目录还有两份跟人设有关：`persona.example.md`（**进仓库**，示例文本）、
`persona.md`（**已 gitignore**，你自己那份；缺了就用内置默认语气）。

> 验收入口是 `tools\run_all.py`（96 项一条命令，并校验断言条数）。
> `tools\smoke_http.py` 是手动联调脚本，不在里面。

## 11. 已知坑（都踩过）

- Windows 中文终端是 GBK：`cli._setup_console()` 在输出被重定向时切 UTF-8；`run.cmd` 必须纯 ASCII。
- **不要用 PowerShell 的 `Set-Content -Encoding UTF8` 改 UTF-8 源文件**——会读成 GBK 再写回，中文全毁。
- SQLite 的 trigram 对中文无效（2 字词返回 0 条）→ 用预分词索引列 + phrase 查询。
- FTS/派生表删除要看清外键级联；`segment_tags` 依赖 `segments` 的 CASCADE。
- 时间比较注意 naive/aware timezone 混用（会 TypeError）。
- `context` 不是严格只读：它 upsert "实时对话"会话行，但**不写消息**。
- **本仓只放代码与文档，不放任何人的记忆**。`data/`、`persona.md`、`secrets.toml`
  都在 `.gitignore` 里；提交前用 `python tools/check_privacy.py` 扫一遍（它跑在
  `run_all.py` 里），别靠人眼。
- **任何脚本都不许写真实库**（除非它明确就是 `pmem` 命令本身）。`smoke_http.py` 曾经直接写，
  跑一次就在 `实时对话 <日期>` 里留下 6 条测试消息 + 3 条路由日志，还会让误查/漏查统计失真。
  现在它先用 `con.backup()` 复制成 `data/_http_smoke.db` 再跑，跑完删除。新脚本照此办理。

## 12. 接手后的第一件事

**不要先改代码。** 先跑第 5 节的全部验收，确认 96 项全绿——那是你判断"我的改动有没有弄坏东西"的唯一基线。

## 13. 发布清单

发布进度（能给别人看了吗、还差什么）单独放在 [`RELEASE.md`](RELEASE.md)——那是给"要发布的人"看的，
和"现在代码什么状态"不是一回事，混在一起两边都读不顺。

当前一句话：**已发布** → <https://github.com/chenmoeqiu/pmem>（`v0.1.0`，CI 5/5 绿）。

---

## 14. 待办 backlog（记录，不动手）

> 这些是**已经想清楚、但决定先不做**的事。写下来是为了别忘了，也是为了别被当成待办催。

| 项 | 为什么现在不做 | 什么时候做 |
|---|---|---|
| **图片与文字分批到达**（合并窗口，**运行时侧**） | pmem 这半边已经做了：热窗标注"附图 N 张（已描述/未描述）"，未描述时明说"看不到，不要猜"。剩下的是"等一小会儿再答"的缓冲——那属于运行时，pmem 决定不了何时回复 | 使用者常发图时。做法：收图后只 `note` 不回复，静默 1.5–3 秒（期间来新消息就重置），窗口结束再走 `context` → 模型 → `note` |
| **整理挪到夜间定点**（谷价） | 现在整理发生在"睡醒后第一轮对话"里，那一刻不是 API 谷价 | `[cold] auto = false` + 每晚 `pmem daily --now`。当前实现已兼容，只换触发方式 |
| Takeout 按 `source_ref` 判重 | 历史库不常重导；判重逻辑约 20 行 | 真遇到重复导入之前（不可逆，`purge` 只能逐条清） |
| 段的合并/拆分命令 | 现在只能整场 `tag rerun`，会丢手工修正 | 有人真的手工改过段之后 |
| C 类指代在**整理阶段**的诊断 | 对话阶段已有候选反问；整理阶段缺自动发现 | 待裁决队列开始积压时 |
| 记忆曲线 / 情绪权重**算法** | 先把 MVP 跑起来；钩子已留（`search(..., weight=)`） | 有真实检索反馈数据之后（现在没有校准依据） |
| CI | 先把仓库推上去 | 建好远端之后 |
