# CookieCloud 只读入口

课代表经公网只读入口读取用户 Chrome 里的 YouTube 登录态。本页是课代表依赖的约定；边缘与 ingress 的配置以 CookieCloud 仓库（`../CookieCloud`）的 `docs/read-ingress.md` 为准。课代表一侧的配置见 [cookiecloud-credentials-ui.md](cookiecloud-credentials-ui.md)。

| 项 | 现值 |
|---|---|
| origin | `https://cookiecloud-read.example.com`（Cloudflare Tunnel，cloudflared 和只读代理跑在懒猫上） |
| 源实例 | prodbox 上的 CookieCloud，Chrome 插件同步到这里 |
| 限流 | 边缘每 IP 每 10 秒 5 次，只读代理每分钟 30 次；都和正常下载共用 |

## 只开放三条路径

| 方法 | 路径 | 课代表怎么用 |
|---|---|---|
| GET | `/api/v3/cookies?format=netscape&domains=youtube.com` | 读 cookies；`domains` 来自设置 `cookiecloud_domains`，默认 `youtube.com` |
| GET | `/api/v3/token/introspect` | 核对 App Token 的名称与域名白名单 |
| GET | `/api/health` | 健康检查 |

其余路径返回 404，其他方法返回 405，所以这个入口创建不了 Token，也配对、上传不了。

## 两道鉴权

每次请求同时带两类凭据，各自能单独吊销：

- **Cloudflare Access 服务令牌**：请求头 `CF-Access-Client-Id`、`CF-Access-Client-Secret`。现用的令牌名叫 `assetizer-ovh`，名字是 OVH 时期留下的，**仍在用，不要吊销**；2027-10-13 到期。
- **CookieCloud App Token**：`Authorization: Bearer ccat_…`。名称 `assetizer`，权限 `cookies:read`，域名白名单 `youtube.com`。

来源 IP 还要在白名单里。白名单在 CookieCloud Setup 的 `/setup/read-allowlist` 维护，不要手改 Access 策略。代理节点换了出口 IP，读取会报 `ip_not_allowed`，令牌不用动，见 [operations.md](operations.md)「凭据与登录」。

## 调用方式

- 每次读元数据或下载前读一次，一次频道扫描只读一次。
- 拿到的 Netscape 文本写成权限 0600 的临时文件，交给 `yt-dlp --cookies`，用完马上删除。cookie 值不进日志、事件或数据库。
- 连接断开，或 Cloudflare 答复连不上源站时，2 秒后重试一次；超时不重试。
- 读不到时按错误码提示（[错误码](cookiecloud-credentials-ui.md#32-错误码)）。懒猫上没有配置服务器 Chromium，读不到就是未登录下载，YouTube 要求登录的视频会进入等待。
- 课代表走公网入口是沿用下来的做法，没有改成直连 `*.heiyu.space`。
