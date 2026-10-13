# 需求：为 Assetizer 开放 CookieCloud 只读接口（Cloudflare Tunnel + Access 服务令牌）

- 提出方：Assetizer（投资内容自动资产化）· 2026-10-13
- 执行方：CookieCloud 项目（`../CookieCloud`）的 agent
- 方案：用户已选定方案 A——Cloudflare Tunnel 只开放读取接口，Cloudflare Access 服务令牌 + CookieCloud App Token 两道鉴权

## 1. 背景

- Assetizer 跑在 OVH VPS（出口 IP `203.0.113.10`，用户 `debian`），用 yt-dlp 下载 YouTube 视频。王海滨频道有会员视频，需要用户本人 Chrome 里的 YouTube 登录态（含频道会员权限）。
- CookieCloud 已部署在懒猫：`cookiecloud.prodbox.heiyu.space`（正式）和 `cookiecloud.devbox.heiyu.space`（开发）。用户 Chrome 上的 `cookiecloud-chrome-mv3` v0.3.1 插件负责把 cookies 同步进去。
- **OVH 不在懒猫网络内，访问不到 `*.heiyu.space`**。2026-10-13 实测：从 OVH 请求两台的 `/api/health` 都连接失败；从用户 Mac 请求都是 200。
- 读取方式沿用 YouTube2Notion 已验证的 V3 只读路径：`GET /api/v3/cookies?format=netscape&domains=…`，加 `Authorization: Bearer ccat_…`。**不需要改 CookieCloud 的接口，也不需要新增接口。**

## 2. 目标

提供一个公网 hostname，让 OVH 上的 Assetizer 能读取 YouTube cookies：
- 只开放读取，不开放管理面和同步面；
- 两道鉴权，缺一不可；
- 每个凭据都能单独吊销。

## 3. 要做的事

### 3.1 公网入口

| 项 | 要求 |
|---|---|
| hostname | `cookiecloud-read.example.com`（可以改，但要把最终值告诉用户） |
| Cloudflare | zone `example.com`；Access team `example-team` |
| 源站 | **Chrome 插件实际同步的那个 CookieCloud 实例**。预计是 `prodbox`，请先在 Setup 里确认最近一次同步的时间和来源，再接源站 |
| Tunnel | 按 `lazycat-cloudflare-ingress` 技能的做法：remotely-managed tunnel，`cloudflared` 跑在私有 ingress 侧，前面有本地 proxy 层 |

> ⚠️ **与 `lazycat-cloudflare-ingress` 的默认做法不同**：那个技能默认「原生 App / relay 入口不上 Access 与 challenge」。这个入口不是原生 App 或 relay，而是**服务器对服务器、返回 Google 账号登录态的敏感接口**，**必须**放在 Cloudflare Access 后面。

### 3.2 只开放三条路径

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v3/cookies` | 读取 cookies（query 原样透传：`format`、`domains`） |
| GET | `/api/v3/token/introspect` | 调试 App Token |
| GET | `/api/health` | 健康检查 |

- 其余路径一律 404，包括 `/setup/*`、`/api/update`、`/api/get/*`、`/api/v3/plugin/connect`、`/api/v2/*`、`/cookie-lab*`。
- 只允许 GET（HEAD 可选），其他方法返回 405。
- 白名单在 ingress 本地 proxy 层做；Cloudflare 规则可以作为第二层，但不能只靠 Cloudflare。

### 3.3 两道鉴权

**第一道：Cloudflare Access**
- 新建 self-hosted 应用，覆盖整个 hostname。
- 策略：action = **Service Auth**，include = 服务令牌 `assetizer-ovh`（新建）。
- 建议再加一条 require：IP `203.0.113.10/32`。
- 没有有效服务令牌，或 IP 不符时，**直接返回 403，不能 302 跳到登录页**（调用方是 API 客户端）。

**第二道：CookieCloud App Token**
- 新建 App Token：
  - name：`assetizer`
  - scopes：`["cookies:read"]`
  - allowed_domains：`["youtube.com"]`
- **先只给 `youtube.com`**。如果 Assetizer 验证后发现会员视频还需要 `google.com`，会再提出来，到时候再加。

### 3.4 边缘配置

- **缓存**：这个 hostname 全部 bypass cache；源站返回的 `Cache-Control: no-store` 要保留。
- **挑战**：不对这个 hostname 启用 JS challenge、Turnstile、Bot Fight。这些会拦截非浏览器客户端。如果 zone 开着 Bot Fight Mode，要对这个 hostname 豁免，或者确认带服务令牌的请求不会被挑战。
- **速率限制**：每分钟最多 30 次。Assetizer 正常每小时只请求几次。
- **日志**：Cloudflare 和 ingress 的日志里不能出现 cookie 值、`Authorization`、`CF-Access-Client-Secret`，也不记录响应体。

## 4. 要交付的东西

**敏感值不要贴进聊天，也不要提交到 git。** 交给用户，由用户写进 OVH 的 secrets 文件。

| 项 | 示例 | 是否敏感 | 放到 OVH 的位置 |
|---|---|---|---|
| origin | `https://cookiecloud-read.example.com` | 否 | `assetizer set cookiecloud_origin '"https://…"'` |
| Access 服务令牌 Client ID | `xxxx.access` | 是 | `~/assetizer/data/secrets/cf-access-client-id` |
| Access 服务令牌 Client Secret | — | 是 | `~/assetizer/data/secrets/cf-access-client-secret` |
| CookieCloud App Token | `ccat_…` | 是 | `~/assetizer/data/secrets/cookiecloud-token` |
| 源实例和插件同步间隔 | `prodbox`，每 10 分钟 | 否 | 写在交付说明里 |

另外写清每个凭据怎么吊销：Access 服务令牌怎么 revoke，App Token 怎么 revoke。

写入 secrets 的方式（用户在 OVH 上执行，每个文件权限 600）：

```bash
umask 077
printf '%s' '<值>' > ~/assetizer/data/secrets/cookiecloud-token
```

后来补上了图形界面：「机房」→「YouTube 会话」→「配置 CookieCloud」可以填写、更换、清除这四项，并测试连接（[`cookiecloud-credentials-ui.md`](cookiecloud-credentials-ui.md)）。两种方式写的是同一组文件，可以混用。

## 5. 验收（在 OVH 上执行）

把结果贴回来时，**不要贴 cookie 内容**，只贴状态码和行数。

```bash
H=https://cookiecloud-read.example.com
S=~/assetizer/data/secrets
CID=$(cat $S/cf-access-client-id); CSEC=$(cat $S/cf-access-client-secret); TOK=$(cat $S/cookiecloud-token)
A=(-H "CF-Access-Client-Id: $CID" -H "CF-Access-Client-Secret: $CSEC")

# 1. 不带任何头 → 403（不能是 302 跳登录）
curl -s -o /dev/null -w '%{http_code}\n' "$H/api/v3/cookies"
# 2. 只带 Access 头 → 401，code = token_not_found
curl -s "${A[@]}" "$H/api/v3/cookies" ; echo
# 3. 两道都带 → 200、text/plain、no-store，并且有 youtube.com 的行
curl -s -D - -o /tmp/cc.txt "${A[@]}" -H "Authorization: Bearer $TOK" "$H/api/v3/cookies?format=netscape&domains=youtube.com" | grep -i -E '^HTTP|content-type|cache-control'
grep -c youtube.com /tmp/cc.txt; rm -f /tmp/cc.txt
# 4. 域名白名单生效 → 403，code = domain_not_allowed
curl -s "${A[@]}" -H "Authorization: Bearer $TOK" "$H/api/v3/cookies?format=netscape&domains=google.com" ; echo
# 5. 其它路径和方法被挡住 → 404 / 405
for p in /setup /api/get/x /api/v2/profiles; do curl -s -o /dev/null -w "$p %{http_code}\n" "${A[@]}" "$H$p"; done
curl -s -o /dev/null -w 'POST /api/update %{http_code}\n' -X POST "${A[@]}" "$H/api/update"
# 6. token 信息与第 3.3 节一致
curl -s "${A[@]}" -H "Authorization: Bearer $TOK" "$H/api/v3/token/introspect" ; echo
```

第 7 项：如果启用了 IP 限制，从非 OVH 的地址（比如 Mac）带着有效服务令牌请求，应该返回 403。

## 6. 不在范围内

- 不改 Chrome 插件，不改 CookieCloud 的 V3 接口，不新增接口。
- 不开放 Setup UI，不暴露同步面（`/api/update`、`/api/get/*`）和插件配对接口。
- Assetizer 这一侧的工作由 Assetizer 自己负责：读取 cookies、写临时文件交给 yt-dlp、读取失败时回退、在「机房」显示状态。

## 7. 考虑过、但没有选的方案：在 OVH 上安装懒猫客户端

- **好处**：不需要开公网入口，OVH 直接访问 `*.heiyu.space`。
- **不选的原因**：
  - OVH 会变成家里私有网络的一个节点。服务器一旦被攻破，所有懒猫应用都会暴露，影响面远大于一个只读接口。
  - 需要在没有图形界面的服务器上常驻懒猫客户端并登录账号，可用性和稳定性都没有验证过。
  - 方案 A 只开放一个只读接口，有两道鉴权，凭据可以随时吊销，出问题时容易收口。

## 8. Assetizer 会怎么调用（方便对方理解调用模式）

- 每次读取元数据或下载前请求一次，频率是每小时个位数。
- 请求：`GET {origin}/api/v3/cookies?format=netscape&domains=youtube.com`，带上 `CF-Access-Client-Id`、`CF-Access-Client-Secret`、`Authorization: Bearer ccat_…`。
- 拿到后写成权限 600 的临时文件，交给 `yt-dlp --cookies`，用完马上删除。
- 失败时，在「机房」按错误码提示原因：Access 403、`token_revoked`、`domain_not_allowed`、`cookie_payload_missing` 等，并回退到服务器上自带的 Chromium 登录态。
