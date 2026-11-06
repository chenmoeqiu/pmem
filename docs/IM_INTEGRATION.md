# 把 pmem 接进 IM（运行手册）

> 给**在 IM 对话里运行的那个 agent** 看的。DSH 侧只负责消息传递，
> **上下文和记忆全部由 pmem 决定**（`DESIGN.md` 第 12 节「接入」）。

## 前提

1. `@xmanrui/dsh-im` 已安装并启用（本机：5.10.0，已在 `profiles/web/package.json` 里）。
2. **关闭 dsh-im 的「上下文增强」**（机器人卡片 → 上下文增强 → 群聊/私聊都关）。
   它会给用户消息插 `<dsh_im_source>` 前缀；如果同时开，来源标记会有两份，
   而 pmem 自己已经有 `conversations.source`。真需要渠道信息时，由调用方通过
   `--source` 传，不要让插件往正文里塞。
3. 确认 pmem 的数据**不是**从 DSH 自己的会话文件导入的——那是 UI 状态，不是记忆来源。

## 每一轮做三件事

```bat
:: ① 拿上下文（只读，不生成）
run.cmd context "<用户这一句>" --json --source qq --session <会话key>

:: ② 把返回的 messages 发给模型（这是完整 prompt，不要再自己拼历史）

:: ③ 把两边的话写回 pmem（时间由 pmem 盖章，调用方无权传时间）
run.cmd note "<用户这一句>" --role user      --source qq --session <会话key>
run.cmd note "<模型的回复>" --role assistant --source qq --session <会话key>
```

### 带图片/附件

IM 收到的图片**就这样进记忆**（附件按 sha256 落盘，挂到那条消息上）：

```bat
run.cmd note "<用户这一句>" --role user --source qq --session <会话key> ^
            --attach "C:\path\to\image.png"
```

- 可以给多次 `--attach`（一条消息多个附件）
- **同一份文件不会被存两份**（内容寻址；重复的只是多一行消息关联）
- 文件不存在不会中断：消息照写，`attachment_errors` 里记一条原因
- 想顺手写描述就加 `--describe`（每张一次视觉调用），否则事后补：
  `run.cmd describe --id <附件id>`，再 `run.cmd search --images <词>` 查

## 会话 key 怎么取

**同一场外部会话必须一直用同一个 key**，否则每次都是新会话，热窗就断了。

| 渠道 | 建议 key |
|---|---|
| QQ 私聊 | `c2c:<用户 openid>`（dsh-im 的 `describeConversation` 会给 `chatId`/`senderId`） |
| Telegram 私聊 | `direct:<chatId>` |
| 群/话题 | `group:<chatId>` 或 `group:<chatId>:<threadId>` |

`--source` 用渠道名（`qq` / `telegram` / `wechat`），只用来区分来源，**不参与检索排序**。

## 会得到什么

- 同一 `(source, session)` 的消息稳定落进同一场 pmem 会话，**跨天续聊不断开**
  （`tools/smoke_ports.py` 第 11–13 项验收）
- 热记忆 = 当前「清醒段」的全部文字（跨天 **AND** 间隔 ≥ 6 小时才开新段）
- 冷检索排除的正好是热记忆那一段，滑出去的内容立刻可检索（不重不漏）
- 检索走**标签主路径 + 全文兜底**；标签由路由从已批准词表里选
- **睡过一觉之后，上一段会自己转冷并被整理**：`note` 写完助手那条时就顺手做掉
  （排队 + 切段 + 打标，每轮最多 `cold.limit` 场，两次之间至少隔 `cold.interval_hours`）。
  **插件不需要为这件事额外调用什么**；想手动补跑或接定时任务时用 `run.cmd daily --now`。

## 已知限制

1. **重复落库风险**：如果 DSH 侧也把这段对话写进自己的 session 文件，那是两份。
   约定：DSH 的会话文件只当 UI 状态，**不进 pmem 的 raw 层**。
2. **离线模式不生成**：没有 API key 时 `context` 仍能算出上下文，但回复是降级文本；
   同时**不会整理转冷队列**（离线标签是空的，标成"已整理"会永久失去打标机会）。
3. **语音/视频没有入口**：`--attach` 收的是文件字节，语音要先转文字再 `note`。

## 验收清单（接完就跑一遍）

```bat
run.cmd context "我上次说的那台相机" --json --source qq --session test-1
run.cmd note "我上次说的那台相机" --source qq --session test-1
run.cmd context "刚才我说了什么" --json --source qq --session test-1   :: hot 里应该有上一句
run.cmd search 相机
```

跑 20 轮真实对话后，用 `run.cmd log --last 20` 看路由记录：
误查（闲聊却去检索）和漏查（该检索却没查）各有多少。
