# CookieCloud 凭据与「机房」面板

「机房」→「YouTube 会话」配置读取入口和三项密钥，测试连接，并用一个会员视频验证 YouTube 登录态。入口的约定见 [cookiecloud-read-ingress.md](cookiecloud-read-ingress.md)。实现在 `src/assetizer/cookies.py`、`src/assetizer/web/app.py`（`/api/cookiecloud/*`）和 `web/src/components/CookieCloud.tsx`。

## 1. 四项配置

| 字段 | 存放 | 格式 | 从哪里来 |
|---|---|---|---|
| `origin` | 设置 `cookiecloud_origin` | `https://` 加 host，不带路径、查询、用户信息或端口 | `https://cookiecloud-read.example.com` |
| `access_client_id` | `secrets/cf-access-client-id` | `^[0-9a-f]{32}\.access$` | Cloudflare Zero Trust →「Access controls」→「Service credentials」新建服务令牌，再把它加进只读入口 Access 应用的策略 |
| `access_client_secret` | `secrets/cf-access-client-secret` | 可打印 ASCII，不含空白，长度 32–128 | 同上，只显示一次 |
| `app_token` | `secrets/cookiecloud-token` | `^ccat_[A-Za-z0-9]{42}$` | [CookieCloud Setup](https://cookiecloud.prodbox.heiyu.space/setup) →「步骤 3：外部应用访问令牌」→ 域名填一行 `youtube.com` →「创建只读令牌」，只显示一次 |

`secrets/` 在数据目录下，懒猫上是 `/lzcapp/var/assetizer/data/secrets`。写 `~/assetizer/…` 不生效：容器里的 `~` 是 `/lzcapp/var/assetizer/home`。

`config.Settings.secret()` 先读环境变量 `ASSETIZER_<NAME>`（如 `ASSETIZER_COOKIECLOUD_TOKEN`），再读文件。每次都重新读，写入后 web 和 worker 不用重启。

吊销：服务令牌在 Zero Trust →「Access controls」→「Service credentials」，App Token 在 CookieCloud Setup。现用的两个凭据的名称和到期时间见 [cookiecloud-read-ingress.md](cookiecloud-read-ingress.md#两道鉴权)。

## 2. 只写不读

- 任何接口和页面都不返回密钥的值或片段；`status()` 每个字段只给 `configured`、`source`（`env` / `file` / 空）和 `updated_at`。
- 表单保存后立即清空，不回填，不放进 localStorage、sessionStorage、URL 或日志。
- 接口都在 `guard` 中间件后面（见 [architecture.md](architecture.md)「访问控制」）。
- 文件是 0600，目录是 0700；先写同目录临时文件，`fsync` 后 `os.replace`。

## 3. 接口

`POST /api/cookiecloud/credentials`：部分更新，请求体 `{origin?, access_client_id?, access_client_secret?, app_token?, clear?: [...]}`。省略或空字符串表示不修改，`clear` 列出要删除的字段。返回和 `status()` 同形的状态。

| 情况 | 状态码 |
|---|---|
| 格式不对、同一字段又改又清、没有要改的内容 | 400，不回显输入值 |
| 该项由 `ASSETIZER_*` 环境变量提供 | 409，写文件不会生效，所以不假装保存成功 |
| 请求体超过 16 KB | 413 |
| 写文件失败 | 500 |

请求体手动解析，不用 pydantic 模型，因为 FastAPI 的 422 会回显输入。保存后清掉上一次读取和视频测试的结果，它们用的是旧凭据。

### 3.1 两个测试

- `POST /api/cookiecloud/test`：读一次 CookieCloud，返回 `{ok, count, hosts}` 或 `{ok, code, fix}`；CookieCloud 接受了令牌时再带上令牌名称和域名白名单。10 秒内只能一次，否则 429。
- `POST /api/cookiecloud/test-video`：用这份登录态读一条 YouTube 视频，最好是会员视频。通过后，等 YouTube 登录或被 YouTube 拒绝下载的期立刻重新排队。20 秒内只能一次。

配好的标准是**测试连接和视频测试都通过**：指示灯「已就绪」只代表读到了 cookie，不代表 YouTube 认这份登录态。

### 3.2 错误码

| 现象 | 来源 | code |
|---|---|---|
| 403，WAF 拦截页，已带服务令牌头 | Cloudflare WAF：来源 IP 不在白名单 | `ip_not_allowed`，并查一次 `cdn-cgi/trace` 记下出口 IP |
| 其他 401 / 403 或重定向，不是 JSON | Cloudflare Access：缺服务令牌或无效 | `access_denied` |
| 429，不是 JSON | Cloudflare 边缘限流 | `rate_limited` |
| 429 JSON | 只读代理限流 | `ingress_rate_limited` |
| 404 | 入口不开放这个路径，通常是 origin 填错 | `not_found` |
| 530，Cloudflare 自己的错误页 | 连不上隧道：cloudflared 可能断了 | `tunnel_down` |
| 502，Cloudflare 自己的错误页 | 隧道在线，连不上只读代理 | `proxy_down` |
| 其他 5xx，Cloudflare 自己的错误页；连接失败或超时 | 隧道或入口不通 | `unreachable` |
| 502 / 504 JSON | 盒子上的 CookieCloud 没响应 | `upstream_unavailable`、`upstream_timeout` |
| 其他 JSON `{error, code, message}` | CookieCloud 本身 | `token_not_found`、`token_revoked`、`missing_scope`、`domain_not_allowed`、`sync_source_not_configured`、`cookie_payload_missing` 等 |
| 读到了，但没有 YouTube 的 cookie | 课代表自己判断 | `no_youtube_cookies` |

每个 code 的中文说明在 `cookies.py` 的 `FIX`，面板和告警都用它。

## 4. 命令行

```bash
lzc-cli docker -- exec cloudlazycatappassetizer-web-1 python -m assetizer.cli cookies
```

读一次 cookies 并报告结果，不输出值；和「测试连接」同一个 `probe()`。
