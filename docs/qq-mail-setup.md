# QQ 邮箱接入：你需要在网页上做的操作

这份文档对应**第 1 步**：定时登录 QQ 邮箱，把新邮件标题打印到 Actions 日志。
除了下面这些开关和密钥，代码部分已经就绪。

---

## 一、QQ 邮箱侧：拿到 IMAP 授权码

> 关键点：IMAP 登录**不能用 QQ 密码**，必须用单独生成的「授权码」。
> 这个授权码等价于邮箱密码，泄漏就等于邮箱被接管。

1. 浏览器登录 <https://mail.qq.com>（用主号或你要接的那个号）
2. 右上角 **设置** → **账户**（新版界面在「账号与安全」旁边，找「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」那一栏）
3. 找到 **IMAP/SMTP 服务**，点 **开启**
4. 按提示验证（一般是发短信到绑定手机，或验证密保 / 微信扫码）
5. 验证通过后页面给出一个 **16 位授权码**，形如 `abcdwxyzefghijkl`
   - **只显示这一次**，立刻复制到安全的地方
   - 如果弄丢了：在同一页面点「删除授权码」重新生成（旧码立即失效）

顺便确认两件事：

- 邮箱**没有**设置「独立密码」干扰登录（有的话先关掉或用它登录测试）
- 该账号没有开「登录保护」到必须每次扫码的程度（QQ 邮箱的 IMAP 不受影响，但 SMTP 发信可能受限，第 4 步写回邮件时再处理）

**企业 QQ 邮箱 / 腾讯企业邮**（`@公司.com`）用的是另一套：主机 `imap.exmail.qq.com`，
授权流程在 exmail 后台，本仓库脚本里用 `IMAP_HOST` 环境变量覆盖即可。

---

## 二、GitHub 侧：建仓库 + 配密钥

### 1. 建一个**私有**仓库

必须私有，原因：Actions 日志里会出现邮件标题和发件人地址，公开仓库等于公开你的收件箱。
（免费账号的私有仓库每月有 2000 分钟 Actions 额度，当前这个频率够用。）

把项目文件按原样传上去，目录结构必须是：

```
.github/workflows/mail-check.yml
scripts/fetch_mail.py
.gitignore
```

### 2. 加两个 Secret

仓库 → **Settings** → 左侧 **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value |
|---|---|
| `QQ_EMAIL` | 完整邮箱地址，例如 `1234567@qq.com` |
| `QQ_AUTH_CODE` | 上面拿到的 16 位授权码 |

两个都用 Secret，不要用 Variable，也不要写进代码。

### 3. 打开写权限（很容易漏）

仓库 → **Settings** → **Actions** → **General** → 拉到最下面 **Workflow permissions**

选 **Read and write permissions** → **Save**

不开这个，脚本能跑，但最后把游标推回 `state` 分支会 403。
（这是仓库设置，不是 `permissions:` 那一段能替代的——两处都要对。）

### 4. 手动跑第一次

**Actions** 标签 → 左侧 `mail-check` → 右侧 **Run workflow** → `limit` 填 **5** → 绿色按钮。

等 30–60 秒，点进这次运行，看 `取新邮件标题` 这一步的日志，应该出现：

```
12:34:56 INFO    连接 imap.qq.com:993（账号 1234567@qq.com）
12:34:57 INFO    登录成功
12:34:57 INFO    已发送 IMAP ID 声明
12:34:57 INFO    当前游标 last_uid=0，发现 812 封未处理邮件
12:34:57 INFO    首次运行：只处理最新 5 封，更早的历史邮件跳过

[1/5] UID=10233  2026-10-25 09:12  张三 <zhangsan@example.com>  |  关于周五评审的时间调整
[2/5] UID=10234  2026-10-25 10:03  系统通知 <noreply@github.com>  |  [repo] New issue #12
...
12:34:58 INFO    游标推进到 last_uid=10237，状态已写入 state/mail_state.json
```

最后一步 `保存游标` 结束后，仓库会多出一个 **`state`** 分支，里面只有
`state/mail_state.json` 一个文件。主分支保持干净。

### 5. 验证"增量"到底work不work

再点一次 **Run workflow**，这次应该看到：

```
INFO  当前游标 last_uid=10237，发现 0 封未处理邮件
INFO  没有新邮件，游标不变
```

然后再往那个 QQ 邮箱**自己发一封测试邮件**，标题写「测试 1」，等下一次定时触发
（或再手动跑一次），应该只看到这一封。

---

## 三、常见报错对照表

| 日志里的关键字 | 真实原因 | 怎么办 |
|---|---|---|
| `Unsafe Login` | IMAP ID 声明没生效，或用了 QQ 密码 | 确认用的是授权码；脚本已内置 ID 命令，若仍报错看是否 `imaplib.Commands['ID']` 被覆盖 |
| `LOGIN failed` / `AUTHENTICATE failed` | 授权码错 / 已失效 / IMAP 服务没开 | 重新生成授权码，确认 IMAP/SMTP 服务是「已开启」 |
| `command ID illegal` | imaplib 不认 ID 命令 | 脚本顶部那行 `imaplib.Commands["ID"] = ...` 必须存在 |
| `SELECT INBOX failed` | 信箱名不对（QQ 的收件箱确实叫 `INBOX`） | 用 `--folder` 指定，或先 `LIST` 看真实名字 |
| `登录过于频繁` / 被踢下线 | QQ 的 IMAP 频率限制 | 把 cron 从 `*/30` 改成 `*/60`，别用 `*/5` |
| `保存游标` 步骤 403 | Workflow permissions 没给写权限 | 回到上面第 3 节 |
| 定时没按点跑 | Actions 的 cron 本来就延迟，负载高时能拖 15–30 分钟 | 正常现象，不要当闹钟用，需要准点就换方案 |
| 中文标题变成乱码 | 解码链没命中（少见） | 看日志里 `decode_mime` 的输出，把 `gb18030` 提到候选第一位 |

---

## 四、这一版刻意**没做**的事

| 没做 | 原因 |
|---|---|
| 取正文 | 第 2 步才需要，而且要先做 HTML 清洗和引用剥离，否则 token 全浪费在签名档上 |
| 任何 LLM 调用 | 先把邮箱链路跑稳，别把两种故障混在一起排查 |
| 标记已读 | `EXAMINE` + `BODY.PEEK` 双保险，保证机器看了一眼不会改变你邮箱的已读状态 |
| 回写 / 发信 | 需要额外授权和人工确认，放后面 |
| 状态存数据库 | 一个 JSON + 一个 `state` 分支足够，等真有多实例需求再说 |

---

## 五、安全提醒

- 授权码只放在 GitHub Secrets，**不要**贴在 issue、commit、聊天记录里
- 一旦怀疑泄漏：QQ 邮箱里点「删除授权码」，旧的立刻失效，再生成新的并更新 Secret
- 用**专用邮箱**接入，别把主邮箱的读写权交给自动化脚本
- 日志里已经有发件人和标题了，所以仓库必须私有——这条没有例外
