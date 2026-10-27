# 需求：在「机房」图形化配置 CookieCloud 凭据

> **状态：已实现**。Assetizer 是 CookieCloud 的只读消费者。「机房」面板配置读取入口的 origin（设置 `cookiecloud_origin`）和三项只写凭据：CookieCloud App Token，以及 Cloudflare Access 服务令牌的 Client ID 与 Secret（存在 `secrets/`，界面不回显）。面板可以测试连接，并用一个会员视频验证 YouTube 登录态；失败时按 CookieCloud 返回的 `code` 显示可读说明。下文的 Access 令牌名和来源 IP 白名单是 OVH 时期的配置，懒猫出口不同，以 Cloudflare Access 的现行策略为准。实现以 `src/assetizer/cookies.py` 和 `web/src/components/CookieCloud.tsx` 为准。

- 提出方：CookieCloud 只读入口的交付方 · 2026-10-13
- 执行方：Assetizer 的 agent
- 前置：[`cookiecloud-read-ingress.md`](cookiecloud-read-ingress.md) 描述的只读入口已上线并通过边缘侧验证

## 1. 背景

CookieCloud 只读入口已经可用：

- origin：`https://cookiecloud-read.example.com`
- 两道鉴权：Cloudflare Access 服务令牌 `assetizer-ovh`（策略要求来源 IP 为 `203.0.113.10/32`）和 CookieCloud App Token `assetizer`（`cookies:read`，`allowed_domains: ["youtube.com"]`）
- 源实例是 `prodbox`，Chrome 插件每 10 分钟同步一次

Assetizer 的读取侧已经实现（`src/assetizer/cookies.py`）：

- 配置项：setting `cookiecloud_origin`、`cookiecloud_domains`；secrets `cookiecloud-token`、`cf-access-client-id`、`cf-access-client-secret`
- `config.Settings.secret()` 先读环境变量 `ASSETIZER_<NAME>`（如 `ASSETIZER_COOKIECLOUD_TOKEN`），再读 `~/assetizer/data/secrets/<name>`。每次调用都重新读文件，写入后 web 和 worker 不用重启就会生效
- 「机房」的「YouTube 会话」卡片（`web/src/pages/Engine.tsx`）通过 `/api/engine` → `cookies.status()` 显示状态
- `assetizer cookies` 命令读一次 cookies，并报告结果（不输出值）

**现在的问题**：三个密钥只能通过 SSH 写文件。用户希望在「机房」页面里直接填写、保存、测试。

## 2. 目标

在「机房」里完成 CookieCloud 凭据的配置、更换、清除和连接测试：

- 密钥**只写不读**：保存后，任何接口和页面都不再返回它的值；
- 不放宽现有访问控制；
- 测试结果用现有的 `FIX` 文案解释。

## 3. 要做的事

### 3.1 后端

所有新接口都放在现有 `guard` 中间件后面，不新增任何豁免路径。`guard` 已经做到：本机 Host 走 SSH 隧道；公网 Host（`kan.example.com`）必须带有效的 Access JWT（该 Access 应用的策略是 owner only）；POST 必须是 `application/json`。

**`POST /api/cookiecloud/credentials`**：部分更新。

- 请求体：`{"origin"?, "access_client_id"?, "access_client_secret"?, "app_token"?, "clear"?: [...]}`。省略或空字符串表示不修改；`clear` 列出要删除的字段。
- 校验。失败时返回 400，错误信息**不得回显输入值**：

  | 字段 | 规则 | 存储 |
  |---|---|---|
  | `origin` | 必须 `https://`，只能有 host（不能带 path、query、userinfo），去掉末尾 `/` | `db.set_setting("cookiecloud_origin", …)` |
  | `access_client_id` | `^[0-9a-f]{32}\.access$` | secret `cf-access-client-id` |
  | `access_client_secret` | 可打印 ASCII，不含空白，长度 32–128 | secret `cf-access-client-secret` |
  | `app_token` | `^ccat_[A-Za-z0-9]{42}$` | secret `cookiecloud-token` |

- 写入：
  - `secrets_dir` 保持 0700；
  - 先用 `O_CREAT|O_EXCL` 和 0600 写同目录临时文件，`fsync` 后用 `os.replace` 替换；
  - 写入后去掉首尾空白（`secret()` 本来就会 strip）；
  - `clear` 直接删除对应文件。
- **环境变量遮蔽**：如果某个 secret 已由对应的 `ASSETIZER_*` 环境变量提供，写文件也不会生效。这时返回 409，说明「该项由环境变量提供，请在服务器上修改」，不能假装保存成功。
- 返回值：和 `status()` 同形的状态，不含任何值。
- **FastAPI 默认的 422 校验错误会把 `input` 原样回显**。这个接口不要用 pydantic 模型直接接收密钥字段，改为手动解析 `await request.json()`，或者给 `RequestValidationError` 加一个处理器，去掉 `input` 和 `ctx`。

**扩展 `cookies.status()`**：每个字段给出 `{"configured": bool, "source": "env" | "file" | null, "updated_at": <文件 mtime 或 null>}`。origin 照常返回（不敏感）。依然不返回任何密钥值，也不返回密钥的片段，比如末四位。

**`POST /api/cookiecloud/test`**：执行一次 `cookies.fetch()`。

- 返回 `{ok: true, count, hosts}` 或 `{ok: false, code, fix}`，口径与 `cmd_cookies` 相同；
- 写 `youtube_cookies_state`，复用 `_record`；
- 可以再请求一次 `GET {origin}/api/v3/token/introspect`，把 `token.name`、`scopes`、`allowed_domains` 一起返回，这些都不敏感；
- 服务端节流：10 秒内只允许一次，超出返回 429。原因见下文：边缘限流是每 IP 每 10 秒 5 次，而且是与正常下载共享的。

### 3.2 补充错误码

只读入口返回的错误类型如下，`cookies.fetch()` 和 `FIX` 要覆盖全：

| 现象 | 来源 | 建议 code |
|---|---|---|
| 401/403，响应不是 JSON | Cloudflare（缺服务令牌头、IP 不符、服务令牌无效） | `access_denied`（已有，逻辑正确） |
| 429，响应不是 JSON | Cloudflare 边缘限流：每 IP 每 10 秒 5 次 | 新增 `rate_limited` |
| 429 JSON，`code: ingress_rate_limited` | 盒子上的 ingress proxy：每分钟 30 次 | 新增 `FIX` 文案 |
| 404 JSON，`code: not_found` | ingress 不开放这个路径，通常是 origin 填错 | 新增 `FIX` 文案 |
| 502/504 JSON，`code: upstream_unavailable` / `upstream_timeout` | 盒子上的 CookieCloud 没响应 | 新增 `FIX` 文案 |
| 其他 JSON `{error, code, message}` | CookieCloud 本身（`token_not_found`、`token_revoked`、`domain_not_allowed`、`cookie_payload_missing` 等） | 已有 |

### 3.3 前端（`Engine.tsx`，「YouTube 会话」卡片）

- 新增「配置 CookieCloud」入口，桌面和手机都能用：
  - 未配置时显示这个入口，代替现在的远程登录提示；
  - 已配置时显示「修改」。
- 表单四个字段：
  - origin：文本框，预填当前值，没有时预填 `https://cookiecloud-read.example.com`；
  - Client ID、Client Secret、App Token：用 `type="password"`、`autocomplete="off"`、`spellcheck={false}`。
- 每个字段旁边显示状态：「已配置 · 文件 · x 前更新」「未配置」或「由环境变量提供（此处不可修改）」。环境变量提供的字段，输入框禁用。
- 留空表示不修改。「保存」成功后立即清空表单 state，**任何时候都不回填密钥**。
- 「测试连接」：成功显示「已读到 N 条」，以及 token 名称和域名白名单；失败显示 `FIX[code]` 和错误码。
- 每个字段都有「清除」，需要二次确认。
- 不把任何值放进 localStorage、sessionStorage、URL 或日志。

## 4. 安全清单

- [ ] 新接口全部经过 `guard`，没有新增 Host、路径或方法豁免
- [ ] 任何响应、日志、事件、alert、数据库里都没有密钥值或片段，包括异常、422 和 400 错误
- [ ] secrets 目录是 0700，文件是 0600，写入是原子替换
- [ ] 被环境变量遮蔽时明确拒绝，不能静默成功
- [ ] 测试接口有服务端节流
- [ ] 前端不缓存、不回填密钥

## 5. 不在范围内

- 不改 CookieCloud，不改 Cloudflare 配置
- 不自动创建凭据：App Token 在 CookieCloud Setup 里建，Access 服务令牌来自 Cloudflare
- 不提供读取密钥值的接口
- 不改现有的 SSH 写文件方式，两种方式并存

## 6. 测试与验收

pytest（`tests/test_core.py` 已有 CookieCloud 测试，在那里补）：

- 保存后文件是 0600、内容正确；部分更新不影响其他字段；`clear` 会删除文件；
- 格式非法返回 400，并且响应体和 `caplog` 里都找不到提交的合成值；
- 被环境变量遮蔽时返回 409；
- `status()` 返回 `source` 和 `updated_at`，并且不含合成的密钥值；
- 测试接口能把 Cloudflare 的非 JSON 401/403/429、proxy 的 JSON 429/404/502/504、CookieCloud 的 JSON 错误分别映射到正确的 code；节流生效；
- `guard`：公网 Host 不带 JWT 返回 403，非 JSON 的 POST 返回 415（原有行为不回退）。

前端：`npm run build`（含 `tsc -b`）通过。

上线后的人工验收：

1. 通过 `kan.example.com` 打开「机房」，桌面端和手机端各试一次。
2. 填写四项后保存，状态变为「已配置 · 文件」。
3. 点「测试连接」，应显示 N 条 youtube.com cookies，token 为 `assetizer`，白名单为 `youtube.com`。
4. 「YouTube 会话」的指示灯变为「已就绪」。

## 7. 凭据从哪里来（给用户）

| 项 | 来源 |
|---|---|
| origin | `https://cookiecloud-read.example.com` |
| Access Client ID / Secret | 用户 Mac 上的 `~/.cache/cookiecloud-read-handoff/cf-access-client-id` 和 `cf-access-client-secret`（0600）。可以用 `pbcopy < 文件` 复制后粘贴，导入后删除该目录 |
| App Token | CookieCloud Setup（`https://cookiecloud.prodbox.heiyu.space/setup`）→「步骤 3：外部应用访问令牌」→ 名称 `assetizer`，域名填一行 `youtube.com` →「创建只读令牌」。令牌只显示一次 |

吊销方式不变：Access 服务令牌在 Zero Trust → Access controls → Service credentials 里吊销 `assetizer-ovh`；App Token 在 CookieCloud Setup 里吊销 `assetizer`。服务令牌于 2027-10-13 过期。
