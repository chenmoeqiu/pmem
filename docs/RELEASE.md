# RELEASE —— 发布清单（0.1.0）

> 这份文件只回答一个问题：**这个仓库现在能不能给别人看，还差什么。**
> 现在到哪一步看 [`HANDOVER.md`](HANDOVER.md)；为什么这么设计看 [`DESIGN.md`](DESIGN.md)。

---

## 已经做完的

| 项 | 说明 |
|---|---|
| 干净副本 | 不含任何真实数据、证据原稿或真实词表；**第一次提交就是干净的** |
| 测试素材中性化 | 约 86 处真实词换成中性写法（地名用甲省/乙市，机构名用「某激光公司」这类写法）。原因：测试代码也是仓库的一部分，会出现在别人的 `grep` 结果里 |
| 合成样例 | `samples/sample_data.json` + `tools/load_samples.py`：20 场对话 / 56 条消息 / 2 张图 / 13 个标签 / 1 条待裁决 / 1 条任职区间，全部虚构 |
| 人类入口 | `README.md`：一句话定位 / 5 分钟跑通 / 一页架构 / 明确不做什么 |
| AI 入口 | `docs/AGENTS.md`：铁律 / 代码地图 / 验收机制 / 常见任务配方 / 坑 |
| 安装形态 | `pyproject.toml` + `pmem` 命令入口；`python -m pmem` 仍然可用 |
| 隐私扫描 | `tools/check_privacy.py`：结构规则（邮箱 / key / 本机路径 / 手机号 / 身份证）+ 可选私有词表，已纳入 `run_all` |
| 跟踪清单检查 | `tools/check_tracked.py`：查 **git 索引**里有没有 `secrets.toml` / `persona.md` / `data/` / `*.db`。`.gitignore` 只防手滑，不防 `git add -f` |
| 验收 | `python tools/run_all.py`：96 项上限，空库时 94 通过 + 2 跳过 |
| CI | `.github/workflows/ci.yml`：Ubuntu + Windows × Python 3.11/3.12 跑 `run_all`；另一个 job 验证 `pip install -e .` 后 `pmem` 入口可用 |
| LICENSE 署名 | `Copyright (c) 2026 ChenMoq` |

## 发布地址

- 仓库：<https://github.com/chenmoeqiu/pmem>
- 首个版本：`v0.1.0`（tag 已推）
- CI：5 个 job 全绿（Ubuntu/Windows × Python 3.11/3.12 + 打包验证）

## 还没做的

目前没有必须做的事。可选的后续见 [`HANDOVER.md`](HANDOVER.md) 第 14 节（backlog）。

## 发布前自查（三条）

1. `python tools/check_privacy.py` → 命中 0 处；`python tools/check_tracked.py` → PASS。
2. `python tools/run_all.py` → 与 `EXPECTED` 的条数一致，没有 FAIL。
3. `git status` 干净；`git ls-files` 里没有 `data/`、`persona.md`、`secrets.toml`。

## 关于版本号

`0.1.0` 是第一个公开版本。schema 是 **v15**（库结构版本，与包版本独立演进）。
升级库结构时：改 `db.py` 的 `DDL` + `_MIGRATIONS`，再 `SCHEMA_VERSION += 1`；老库打开时自动迁移。
