# 从零接入：QQ 邮箱 + 模型 + 群机器人

这份文档是**手把手版**。只想看项目是干什么的，回 [README](../README.md)。
下面的每一步都在真实环境验证过，报错对照表在最后。

整体顺序：**邮箱授权码 → 模型 key → 群机器人 → GitHub Secrets → 首次运行**。

---

## 一、QQ 邮箱：拿 IMAP 授权码

> 关键点：IMAP 登录**不能用 QQ 密码**，必须用单独生成的「授权码」。
> 这个授权码等价于邮箱密码，泄漏就等于邮箱被接管。

1. 浏览器登录 <https://mail.qq.com>
2. 右上角 **设置** → **账户**（新版界面在「账号与安全」旁，找
   「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」那一栏）
3. 找到 **IMAP/SMTP 服务** → 点 **开启**
4. 按提示验证（短信 / 密保 / 微信扫码）
5. 页面给出 **16 位授权码**，形如 `abcdwxyzefghijkl`
   - **只显示这一次**，立刻存到密码管理器
   - 弄丢了：同页面「删除授权码」重新生成，旧码立即失效

顺便确认：

- 邮箱没有设置「独立密码」干扰登录
- 用的是**专用邮箱**（能承受"里面所有邮件都被一个脚本读"的邮箱），别用主邮箱

**腾讯企业邮**（`@公司.com`）是另一套：主机 `imap.exmail.qq.com`，
授权在 exmail 后台，脚本里用 `IMAP_HOST` 环境变量覆盖即可。

---

## 二、模型：拿一个 OpenAI 兼容的 key

默认接 DeepSeek：

1. <https://platform.deepseek.com/api_keys> → 创建 API key（`sk-...`）
2. 记下 base URL：`https://api.deepseek.com`
3. 模型名：`deepseek-chat`

任何 OpenAI 兼容服务都能用（OpenAI、Moonshot、本地 Ollama 代理等），
只要填对 `LLM_BASE_URL` / `LLM_MODEL`。脚本只依赖 `/chat/completions` 这一个接口。

> 成本参考：一次请求塞 6 封邮件、正文截到 3000 字，输入约 3–6k tokens。
> 一天 20 封邮件、跑 20 次增量，月成本通常在几元人民币量级。
> 噪音邮件走规则不花钱，`--limit` / `--max-body-chars` 是成本护栏。

---

## 三、群机器人：拿 Webhook 地址

### 企业微信

群聊 → 右上角 `...` → **消息推送 / 群机器人** → **添加机器人** → 起个名字 →
复制地址：`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx`

### 飞书

群设置 → **群机器人** → **添加机器人** → **自定义机器人** → 复制地址：
`https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`

脚本按域名自动识别渠道：

| 渠道 | 消息形态 | 备注 |
|---|---|---|
| 企业微信 | `markdown` | 单条上限约 4096 字节，脚本按 3500 字节切段 |
| 飞书 | `interactive` 卡片 | 有 P0 时红色标题；单卡最多 30 个元素，超了拆卡 |
| 其他地址 | `{"text": "..."}` | 只要收 JSON 的服务都能接 |

> 只推 **P0/P1**。P2/P3 留在日报里，不打扰你。

---

## 四、GitHub 侧：Secret + 权限

### 1. Secret / Variable

仓库 → **Settings** → **Secrets and variables** → **Actions**：

| 类型 | Name | Value |
|---|---|---|
| Secret | `QQ_EMAIL` | 完整邮箱地址，如 `1234567@qq.com` |
| Secret | `QQ_AUTH_CODE` | 16 位授权码 |
| Secret | `LLM_API_KEY` | `sk-...` |
| Secret | `WEBHOOK_URL` | 群机器人地址 |
| Variable | `LLM_BASE_URL` | `https://api.deepseek.com` |
| Variable | `LLM_MODEL` | `deepseek-chat` |

密钥一律用 Secret，不要写进代码、commit、issue。
`LLM_BASE_URL` / `LLM_MODEL` 不是密钥，用 Variable 就行。

### 2. 打开 Workflow 写权限（最容易漏）

仓库 → **Settings** → **Actions** → **General** → 最下面 **Workflow permissions**
→ 选 **Read and write permissions** → **Save**

不开这个，取信和分级都正常，但最后把游标推回 `state` 分支会 **403**。
（这是仓库设置，和 workflow 里 `permissions: contents: write` 是两回事，两处都要对。）

### 3. 首次运行

**Actions** → 左侧 `mail-check` → **Run workflow** → `limit` 填 **5** → 运行。

首次运行是"冒烟测试"：只处理**最新 5 封**，然后把游标推到信箱最大 UID，
不回溯历史邮件（否则第一次就会吐出几千封）。

日志应该是这样（发件人被打码，因为 CI 里 `LOG_MASK=1`）：

```
INFO    连接 imap.qq.com:993（账号 1***@qq.com）
INFO    登录成功
INFO    当前游标 last_uid=0，发现 812 封未处理邮件
INFO    首次运行：只处理最新 5 封，更早的历史邮件跳过
[1/5] UID=10233  2026-10-25 09:12  张*** <z***@example.com>  |  关于周五评审的时间调整
INFO    模型消耗：输入 4120 tokens，输出 610 tokens
...
INFO    投递 wecom：成功 —— 已送达
INFO    日报已写入 .../digest/20261025-091230.md
INFO    游标推进到 last_uid=10237，状态已写入 state/mail_state.json
```

运行结束后：

- **Summary 页**会显示当天的 Markdown 日报（含 P2/P3，但发件人已打码）
- **Artifacts** 里有 `mail-digest-<run_id>`（保留 14 天）
- 仓库多出一个 **`state` 分支**，里面只有 `state/mail_state.json`，主分支保持干净

### 4. 验证"增量"真的增量

再点一次 **Run workflow**：

```
INFO  当前游标 last_uid=10237，发现 0 封未处理邮件
INFO  没有新邮件，游标不变
```

然后给自己发一封标题含「测试 1」的邮件，等下一次定时触发（或再手动跑一次），
应该只看到这一封。

### 5. 定时策略

`.github/workflows/mail-check.yml` 里两条 cron（**GitHub 的 cron 是 UTC**）：

| cron | 北京时间 | 含义 |
|---|---|---|
| `*/30 0-15 * * 1-5` | 工作日 08:00–23:30 | 每 30 分钟 |
| `0 2,8,14 * * 6,0` | 周末 10:00 / 16:00 / 22:00 | 一天三次兜底 |

周末不空窗，是因为周末来的客户邮件更需要有人看见。
`concurrency` 保证同一时间只有一个实例在读写游标。

> Actions 的定时任务本身会延迟，负载高时能拖 15–30 分钟。别当闹钟用。

---

## 五、报错对照表

| 日志关键字 | 真实原因 | 怎么办 |
|---|---|---|
| `Unsafe Login` | IMAP ID 声明没生效，或用了 QQ 密码 | 用授权码；脚本已内置 `ID` 命令，别覆盖 `imaplib.Commands["ID"]` |
| `LOGIN failed` / `AUTHENTICATE failed` | 授权码错 / 失效 / IMAP 服务没开 | 重新生成授权码，确认服务是「已开启」 |
| `command ID illegal` | imaplib 不认 ID 命令 | 脚本顶部 `imaplib.Commands["ID"] = ...` 必须存在 |
| `SELECT INBOX failed` | 信箱名不对 | QQ 的收件箱叫 `INBOX`；用 `--folder` 指定 |
| 被踢下线 / 登录过于频繁 | QQ 的 IMAP 频率限制 | cron 从 `*/30` 改成 `*/60`，别用 `*/5` |
| `保存游标` 步骤 403 | Workflow permissions 没给写权限 | 见上面第四步第 2 节 |
| `企业微信 errcode=93000` | Webhook key 失效或被删 | 群里重新添加机器人，更新 `WEBHOOK_URL` |
| `企业微信 errcode=45009` | 触发频率限制（每分钟 20 条） | 减少推送量：`--notify-min P0` |
| 退出码 4 | 模型调用**全部**失败 | 看上面日志里每批的错误（429/401/超时）；单批失败不会返回 4 |
| 退出码 5 | 投递失败（webhook/日报） | 修好之后下一轮会自动重试同一批（游标没推进） |
| 中文标题乱码 | 解码链没命中（少见） | 极少见；日志里能看到 `decode_mime` 的原始输出 |
| 定时没按点跑 | Actions cron 延迟 | 正常现象，见上面的提示 |

---

## 六、安全提醒

- 授权码只放 GitHub Secrets，**不要**贴进 issue、commit、聊天记录
- 怀疑泄漏：QQ 邮箱「删除授权码」，旧码立即失效，再生成新的更新 Secret
- 用**专用邮箱**接入，别把主邮箱的读写权交给自动化脚本
- 本仓库是公开的，所以：CI 里 `LOG_MASK=1` 打码发件人，日报只进 artifact（14 天）
  和运行摘要，不落进仓库
- 邮箱内容本身只在你自己的 Actions 运行里出现，不会被提交
