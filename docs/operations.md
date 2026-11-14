# 运维：部署、升级与排障

课代表以懒猫 LPK 运行（包名 `cloud.lazycat.app.assetizer`，版本在 `package.yml`）。两台盒子：

| 盒子 | 用途 | 入口 |
|---|---|---|
| prodbox | 生产（日常使用，生产与交付都在这里跑） | https://assetizer.prodbox.heiyu.space |
| devbox | 开发机，验证安装包 | https://assetizer.devbox.heiyu.space |

两台各有独立的数据库和凭据。devbox 的 Notion token 是开发用的 integration「课代表（开发）」（2026-11-10 起），只连了「🧪 Assetizer 测试交付（临时 token，可删除）」这一页，看不到正式库。

## 发版

1. 确认当前分支包含生产上正在跑的版本：`origin/main` 可能落后生产，从落后的分支打包，安装后功能会退回去。改 `package.yml` 的 `version` 并提交（提交标题沿用「版本 x.y.z：一句话说明」）。
2. 打包：

   ```bash
   sh scripts/lzc/release.sh
   ```

   脚本先跑 `lzc-cli project release`（构建前端、复制 `src/`、`assets/`、`scripts/lzc/` 到 `dist/`），再用 buildx 重新导出运行时镜像并替换进包里（lzc-cli 2.0.9 导出镜像层可能截断），最后 lint 并打印 sha256。产物在 `.pkgout/cloud.lazycat.app.assetizer-v<版本>.lpk`，旧版本的包留着用于回滚。
3. 选盒子：`lzc-cli box switch devbox`（或 `prodbox`）。默认盒子是本机全局设置，用完切回 prodbox。
4. 升级生产前：
   - 看有没有正在跑的运行（升级会重启 worker，进行中的阶段会从检查点重来）：

     ```bash
     lzc-cli docker -- exec cloudlazycatappassetizer-web-1 python -m assetizer.cli status
     ```

   - 备份数据库到数据卷里的 `backup/`：

     ```bash
     lzc-cli docker -- exec cloudlazycatappassetizer-web-1 python -c "import os, sqlite3; d = '/lzcapp/var/assetizer/data'; os.makedirs(d + '/backup', exist_ok=True); a = sqlite3.connect(d + '/assetizer.db'); b = sqlite3.connect(d + '/backup/assetizer-pre-<版本>.db'); a.backup(b)"
     ```

5. 安装：

   ```bash
   lzc-cli lpk install .pkgout/cloud.lazycat.app.assetizer-v<版本>.lpk --apk n
   ```

   盒子先卸载旧版再装新版，装完后容器要等第一次带登录的访问才启动：`docker ps` 里一时没有 assetizer 容器是正常的，先在浏览器登录打开入口，再验收。没打开之前 worker 不跑，收录和生产都停着。

6. 验收：
   - `lzc-cli docker -- ps -a | grep assetizer`：app / web / worker 三个容器都是 healthy，hook-tunnel 在运行（它不设网络 healthcheck）；
   - 版本：`lzc-cli docker -- exec cloudlazycatappassetizer-web-1 grep ^version /lzcapp/pkg/package.yml`；
   - worker 日志：`lzc-cli docker -- logs --tail 200 cloudlazycatappassetizer-worker-1`；
   - 打开入口，看刊、频道、专题、机房、设置五页。

   `lzc-cli app status` 的 `Installed` 只说明装上了；lzc-cli 2.0.9 的 `app log` 未实现。

**回滚**：重装上一个版本的 LPK。数据库迁移只补列、表和索引（`db.py` 的 `_migrate`；另把重复排队的同一期同一版本试跑取消到只剩一条），旧版本能读新库。需要恢复数据时先停应用，再用备份替换 `assetizer.db` 并删掉旁边的 `-wal` / `-shm`。
例外：装过未合入的第一版网盘备份（`videos` 上带 `backup_*` 列、设置里有 `gdrive_inventory`，目前只有开发机）的库，升级到本版时会把这些旧列和旧清单搬进 `video_backups` 后删掉；这类库回滚到那一版会丢网盘确认、待核对和坏 md5 状态，要先停 web、worker，再用升级前的数据库备份替换。恢复备份会丢升级后新增的规则、副本记录和其他数据库变更，网盘上的文件不受影响。

删除中途中断后由后台核对目录。核对不了时，本地原片会一直保留，低水位也不清，直到核对出结果；这不适用普通的最多 7 天宽限。设置页显示登录失效时先重新登录，后台才会继续核对；查询失败按期退避，最长 30 分钟。

## 容器里的命令行

```bash
lzc-cli docker -- exec cloudlazycatappassetizer-web-1 python -m assetizer.cli <命令>
```

常用：`status`（队列与最近的运行）、`doctor`（ffmpeg、ffprobe、agy、yt-dlp、磁盘、Notion token、Telegram、服务器 Chromium 配置；加 `--agy` 才探测默认 provider 的登录，会调用一次模型；不查 CookieCloud 和分给别家的工序）、`cookies`（探测 CookieCloud）、`set <键> <值>`（设置页能改的设置也可以这样写，键和范围见 `knobs.py`，例如 `set worker_slots 2`）、`submit <链接>`、`deliver <视频 id>`、`telegram-setup`、`notify-digest`、`logs`（只读导出日志，见下面「日志与迭代」）。

新盒子的 `init` 不能在容器里跑：LPK 不含 `seeds/`（`scripts/lzc/build_content.sh`），`init` 要读它。

队列由 worker 自动暂停和恢复，不要手动设置 `queue_paused`。暂停期间 worker 每 10 分钟探测一次在用的 provider（`health()`，会调用模型；agy 每个在用的额度组问一次），都通过就恢复队列、解除这条告警。「读不了视频」不走这条路：它由空闲时的读视频自检解除，每小时一次，通过后等它的运行才重新排队。需要立即恢复时，用首页的「恢复队列」或机房的「手动恢复」：它们清掉暂停，把等登录和等额度的运行重新排队，解除额度告警。按钮不解除 agy 的登录告警，它要等上面的探测，或 agy 回答 `/usage`（恢复时会在后台问一次）；模型调用报「未登录」时 worker 也先问一次 `/usage`，答得上就只让这一期等网络，不暂停生产；因 agy 登录暂停时页面只给「登录 agy」。在终端里登录之后，点机房额度卡的「刷新」，读到额度即解除告警、恢复队列。

## 日志与迭代

agy 和 yt-dlp 更新得勤，改适配代码时要参考它们最近的表现。课代表保留一个月的日志（`logs.py`），每天清理一次过期的：

| 日志 | 在哪 | 留多久 |
|---|---|---|
| yt-dlp 每次下载的输出，读视频信息失败时的输出 | `logs/yt-dlp/<UTC 时间>-<视频 id>-<download 或 metadata>.log` | 30 天（`ASSETIZER_LOG_RETENTION_DAYS`） |
| 每次模型调用的报错与原始输出（`stderr.log`、`raw-output.jsonl`、`prompt.md` 等） | `runs/<运行>/calls/<工序>/attempt-N/`，编译在 `compile/` 下 | 30 天；回执 `invocation.json` 和回答 `response.txt` 不删，重试时复用 |
| 运行事件（`events` 表） | 数据库 | 30 天；还在排队、在跑或在等的运行的事件不删 |

运行、工序明细和模型调用记录（`model_calls`）是产品数据，不随日志删。

每周一的日报多几行「🔧 迭代」：过去 7 天失败、等待、模型调用出错各几次（按原因），部分完成几期，有没有前一个月没见过的报错。运行级的网络抖动、直播没结束、视频不可用、清空时停下、太短不收不算；模型调用的 `PROVIDER_TRANSIENT`、`PROVIDER_TIMEOUT` 照样计数。合计 5 次以上，或出现了新的报错，就写「日志多了，可以让开发用的 agent 扫一遍」。

**扫一遍**由开发者用的 agent 主动发起：Mac 上的 Claude Code 每周一有定时任务按下面做，别的 agent 也照这个做。

1. `lzc-cli box default` 确认默认盒子是 prodbox；不要 `box switch`。
2. 只读导出：

   ```bash
   lzc-cli docker -- exec cloudlazycatappassetizer-web-1 python -m assetizer.cli logs --summary
   lzc-cli docker -- exec cloudlazycatappassetizer-web-1 python -m assetizer.cli logs > /tmp/assetizer-logs.json
   ```

   `--summary` 是这一周的读数，`many` 为 true 才往下看；不加 `--summary` 是保留着的全部日志（`runs` 失败与部分完成的运行、`calls` 出错的模型调用带 stderr 末尾、`yt_dlp` 的原话、`events` 里的警告和错误），已去掉 token、cookie 和链接里的密钥。导出有上限（运行 300、调用 200、yt-dlp 100、事件 500，每个文件只取末尾 3000 字），只有 agy 产生 `stderr.log`，magpie 和兔子的 stderr 为空，编译调用不进 `model_calls`，所以没出现不等于没发生。
3. 归类，对照代码判断要不要迭代：
   - yt-dlp：`YTDLP_OUTDATED`、`YOUTUBE_REFUSED` 变多，看 `yt_dlp` 里的原话，对照 `docker/requirements.lock` 的版本和 `youtube.py` 的 `classify()`；被判成 `UNKNOWN` 的新说法要登记进 `classify()`。
   - agy：按 `agy_version` 看 `calls`，新版本之后才出现的错误（读不了视频、越界读文件、输出不合规、超时），对照 `providers/agy.py`（`MEDIA_READ`、`classify_error`）和 `llm.py`。
   - 其他 `UNKNOWN` 失败：看 `runs` 里的 `error` 和同一运行的 `events`。
4. 写结论：要不要迭代、改哪里、依据（报错原话、次数、版本）。扫描只读：不安装、不升级、不改设置、不试跑，不读凭据文件，不调用模型。要改代码就在仓库里照常改和测，发版要用户同意。

## 凭据与登录

凭据只能由人放置，不进 Git，不出现在日志里。

- **Google 网盘备份（频道与专题，可选）**：用户自建 Google Cloud 项目 `assetizer-backup`，应用名「课代表」，OAuth 客户端选桌面应用、发布状态为正式版，只申请 `drive.file`。在「设置」→「网盘备份」由用户本人填客户端 ID 和密钥（两项一起填、只写不读），再点登录，在自己的浏览器同意后，把地址栏的整个 `http://127.0.0.1:53682/…` 地址贴回（本机打不开是正常的），也接受只贴授权码。授权链接有效 15 分钟，使用 state 和 PKCE。最后建一次备份文件夹，默认「课代表/专题备份」；只能用应用自己建的文件夹，之后按文件夹 ID 找，网盘里移动或改名不受影响。
  设置页按作者（YouTube 频道 ID，会员 / 公开各一项）和专题选择传 / 不传；没选是待定，暂不传且不挡读片。任何适用规则说传就传，归档专题照算，移出的不算。首次迁移现有专题、频道列表中作者的两项都预填传，只在专题出现的作者待定；此后新建不再预填。专题保存原片和底稿（含截图、不含 `calls/`），频道只保存原片；同一视频共用一份远端副本。两条线下载遇到下架、登录、拒绝或工具过期时尝试从网盘取回原片，不看当前规则；频道仍校验至少 720p。
  关规则仅停止以后上传。设置页「可删除」只列全部适用规则明确不传的已核对副本，待定不列；确认后后台逐期复核，将整个 `<视频id>/`（原片、底稿）移到 Google 网盘回收站。应用不彻底删除、不清空回收站，清空回收站后才腾出空间；30 天内可在网盘中恢复。已下架且本机也无原片的单列，默认不勾；本机无原片又无法确认 YouTube 现状时，也保守放入该组。下架事实在断开网盘期间同样保存。删除前持久记录意图；删除中断时，先核对记录的文件夹和身份，核对失败则保持未决、保留本地内容，不能据旧确认清理。文件夹或身份变化时跳过，规则修改和最终删除互斥，等待规则锁超过 3 秒返回 409，稍后再试。删除后应用不再算已备份；改回传且本地仍有内容会重新排队。本地清理最多为待定或未完成备份多留七天，磁盘低水位照常清理；只有要求传却未完成的才记漏备份。待定宽限只在刊首页提醒。
  客户端和登录凭据仅存在 `secrets/rclone.conf`（0600，目录 0700，原子更新），web 和 worker 共用；Agent 不读取、不复制这个文件，也不通过环境变量提供客户端或 token。接口、事件和日志只显示脱敏状态，不返回密钥或 token。登录失效时回设置页重新登录（自己的客户端和文件夹 ID 保留）；「断开」只移除 token，要彻底撤销请到 Google 账号的「安全性」→「与第三方应用和服务的关联」找到「课代表」撤销访问。清除客户端会删除整个本地配置，网盘上的副本仍保留。每次 rclone 调用使用 `secrets/` 下随机命名的私有目录 `.run-<id>/`（0700，放配置副本，rclone 刷新 token 时的临时文件和旧文件也落在里面）和旁边的 `.run-<id>.lock` 租约（0600）。创建、清理和合并刷新 token 时短暂独占主配置；数据库启动迁移完成后，读取网盘状态完全只读，不归集元数据、不重排队列；空规则更新不重置失败退避。调用持有共享卷上的 `flock` 租约，跨容器不依赖 PID；调用结束后删除自己的目录和租约。进程被杀留下的目录和租约，在下一次任何网盘调用、断开、清除客户端或 worker 重启时清掉，仍持有租约的调用不受影响；前提是被杀进程的 rclone 子进程也已退出（容器里 python 一退出 tini 就退出、容器停止），否则它之后刷新 token 会重新写出目录，要等再下一次清理。删不掉的残留（卷只读、文件被锁）跳过并记一条事件，不挡住断开和 worker 启动。主 token 仍与开始时相同才回写刷新结果。失败只在它用的登录（客户端加 refresh token）仍是当前登录时才记：同一次登录里 token 被刷新不算换登录；判断和记录在同一把锁内，断开、重新登录或换客户端之前开始的调用，失败不改新登录的状态、不记失败也不记事件，这一期留在队列里。断开立即移除主 token，正在进行的传输可以用副本完成，但不会再开始新的调用，也不会把 token 写回主配置。断开时正在传的那一期不算失败，重新登录后继续，已传完的原片先核对上传前记下的 md5、不重传；核对失败继续给清理宽限。查询/建目录超时 120 秒，复制与删除超时 6 小时，上传不阻塞其他期查询或网页测试。
  没验证的：真实懒猫 web、worker 两容器之间共享卷上的 `flock` 是否互斥，仍需上机确认。
  rclone 固定版本在 `docker/runtime.Dockerfile`：升级同时修改 `PINNED_RCLONE_VERSION` 和对应 linux-amd64 zip 的 `PINNED_RCLONE_SHA256`，从官方 SHA256SUMS 核对（构建参数不能用 `RCLONE_` 开头：rclone 会把这类环境变量当参数读）；web 与 worker 的 `ASSETIZER_RCLONE` 都指向 `/usr/local/bin/rclone`（开发时可指定其他二进制）。换 token 的 httpx 与 rclone 沿用容器代理，不用 rclone 自带 OAuth 客户端或 `rclone authorize`。未安装 rclone、未填客户端、未登录或未建文件夹时备份层不工作。

- **Notion token**：`/lzcapp/var/assetizer/data/secrets/notion-token`。在设置 → Notion「Token」点「放」或「换」，粘贴后保存：盒子先问一次 Notion 这是哪个 integration（`/users/me`，单次、5 秒），Notion 不认就提示「token 不对」、不保存；存下后「Integration」一行写它的名字，「测试」再问一次。每台盒子用自己的 integration（生产是「课代表」），换 integration 后课代表靠回复开头的「〔课代表〕」认出自己以前的回复。也可以在自己的终端里执行下面这条，粘贴 token，回车后按 Ctrl-D；都不用重启：

  ```bash
  lzc-cli docker -- exec -i cloudlazycatappassetizer-web-1 sh -c 'umask 077; cat > /lzcapp/var/assetizer/data/secrets/notion-token'
  ```

  integration 必须被加到目标库（或其上级页面）的 Connections 里，频道页「换库」才看得到它。
- **agy（模型）登录**：首页或机房告警「模型登录已失效，生产已暂停」时，点告警上的「登录 agy」：web 在伪终端里启动 agy 的登录界面并选 Google OAuth，页面给出授权链接；在 Google 登录后，把授权页给出的授权码（或整段回调地址）贴回面板。登录态和 agy 本身都在持久卷 `/lzcapp/var/assetizer/home` 里，web 与 worker 共用；确认登录以 `agy -p /usage` 能回答为准，成功后告警解除、因登录暂停的队列继续。链接 10 分钟内有效，关掉面板不中止，重新打开会接上。网页登录不成时，进 worker 容器运行 agy：

  ```bash
  lzc-cli docker -- exec -it cloudlazycatappassetizer-worker-1 /lzcapp/var/assetizer/home/.local/bin/agy
  ```

  网页只处理「没登录」的情况；agy 已登录时不会退出重登，换 Google 账号仍用上面的命令行（在 agy 里 `/logout` 再登录）。
- **agy 额度**：`agy -p /usage --output-format json` 由 agy 自己回答，不开对话、不耗额度。worker 在 agy 在用时每 15 分钟读一次（设置 `agy_quota`；一周走势 `agy_quota_logs`，按 Gemini 额度组分开存。0.4.35 及以前的 `agy_quota_log` 是一条列表，升级后第一次读额度时接进当时默认型号的组，之后不再写，保持原样留给回滚），机房的「刷新」立即读一次。magpie 的订阅与模型由 worker 每 5 分钟从网关读取（设置 `magpie_state`，不问供应商、不花额度）；Key 被限制时退回最近一次调用回执里的 `codex.rate_limits`（设置 `magpie_quota`）。agy 的调用每 5 分钟上报给 magpie 的 Usage 页（`rid` 是 `assetizer-<id>`，重复发送会被跳过），模型名和级别分开写（`gemini-3.8-flash` + `high`），否则 magpie 算不出价格。
- **magpie 和兔子的密钥**：`secrets/magpie-api-key`、`secrets/tuzi-api-key`，告警「模型密钥失效」时放这里，恢复后队列自动继续。放法和 Notion token 一样（只有用到时才需要）：

  ```bash
  lzc-cli docker -- exec -i cloudlazycatappassetizer-web-1 sh -c 'umask 077; cat > /lzcapp/var/assetizer/data/secrets/magpie-api-key'
  ```

  地址和模型在设置 `magpie_url`、`magpie_model`、`tuzi_model`；哪道工序用哪个模型在「设置」→「模型分工」（设置 `stage_providers`，默认 provider 是 `llm_provider`；每格的思考强度是 `efforts`，没设的按代码）；最后一格「底稿」是频道调试期的底稿（设置 `channel_draft`，没设时用 agy 列出的 Gemini 4 Carbon high，没有就跟理解走；design §R-21）；Gemini 的型号列表来自 `agy models`（设置 `agy_models`），`agy` 这个值指环境变量 `ASSETIZER_MODEL`。每种错误重试几次在「设置」→「自动重试」（设置 `retry_counts`，只存和默认不同的；`safety_fallback` 是还被拦时换的模型，`off` 不换；design §R6-10）。

- **YouTube**：用 CookieCloud，在「设置」→「YouTube」里按面板步骤配置（[cookiecloud-credentials-ui.md](cookiecloud-credentials-ui.md)）。容器走的代理节点换 IP 后，读取会报 `ip_not_allowed`：令牌不用动，把面板显示的出口 IP 加进 CookieCloud Setup 的 `/setup/read-allowlist` 并同步，旧 IP 删掉。

## Notion 目标与测试副本

- 生产上的两个频道目前都写入正式库「知识领袖长期追踪库」。
- 「🧪 知识领袖长期追踪库（测试副本）」在 Notion 页面「🧪 Assetizer 测试交付（临时 token，可删除）」下，是开发期用临时 token 建的。生产 integration 默认看不到它；要用它做验收，先把那个页面加进生产 integration 的 Connections。它没有「视频链接」属性，写入能成功，按视频查重会失效。
- 涉及定稿或交付的验证不要在正式库上试：用测试副本（先完成上面的 Connections），或在放好 token 的 devbox 上验证。

## 不经网关直接看容器里的界面

排障或截图时，可以用 SSH 隧道连到 web 容器。web 放行本机回环地址，所以不需要懒猫登录。注意：经隧道做的操作就是对那台盒子的真实操作。

```bash
ssh root@devbox.heiyu.space '/lzcsys/bin/lzc-docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" cloudlazycatappassetizer-web-1'
ssh -N -L 18770:<上一步的地址>:8770 root@devbox.heiyu.space
```

然后打开 http://127.0.0.1:18770 。prodbox 同法，换掉主机名。到 prodbox 的 SSH 偶尔会断开，长时间使用时套一层自动重连。盒子上的规则（`/AGENTS.md`）：lzcapp 容器用 `lzc-docker` 管理；不要从 `/`、`/data`、`/lzcsys/data` 递归扫描；找数据目录前先读 `/lzcsys/data/README`。

## 容器出站

`lzc-manifest.yml` 给 web 和 worker 配了代理 `http://172.18.0.1:17896`，`.heiyu.space`、`.lazycat.cloud`、`web`、`worker` 和本机不走代理（`NO_PROXY`）。yt-dlp、CookieCloud、Google OAuth 换 token 和 rclone 默认走它，所以 `ip_not_allowed` 看的是代理节点的出口 IP。magpie、兔子和 GPU 转写绕过代理直连（`providers/magpie.py`、`providers/tuzi.py`、`media/asr.py`）。

## GPU 转写（局域网 4070 Ti）

whisper 转写可以交给局域网里的 Windows 显卡机（RTX 4070 Ti，`192.168.1.50`）。实测 59 分钟音频，GPU 用 51 秒，space 的 CPU 用 29 分钟；文字一致度约 95%，差异主要在语气词上。

- **显卡机**：跑在 WSL Debian 13（`debian-gpu`，镜像网络模式，直接用 192.168.1.50）里，不在 Windows 原生环境里跑。同一段 59 分钟音频，WSL 用 39 秒，Windows 原生用 50 秒。`scripts/gpu_asr/server.py` 放在 `~/voice-lab/asr`，环境是 `~/voice-lab/envs/asr`（faster-whisper、nvidia-cublas-cu12、nvidia-cudnn-cu12 9），模型在 `~/voice-lab/models/hf`，令牌在 `~/voice-lab/asr/token`，监听 8790 端口。服务用 systemd 管理（`scripts/gpu_asr/assetizer-asr.service` → `/etc/systemd/system/`），日志用 `journalctl -u assetizer-asr` 查看。模型空闲 10 分钟后自动卸载，释放显存。
- **常驻**：WSL 开了 systemd，`.wslconfig`（`%USERPROFILE%`）设了 `instanceIdleTimeout=-1` 和 `vmIdleTimeout=-1`，没有会话连着也不会关机；Windows 登录时，计划任务「WSL debian-gpu autostart」会启动发行版，服务随之起来。Windows 重启后如果没有人登录，WSL 不会启动，转写会退回 CPU。
- **端口坑**：镜像网络下，WSL 和 Windows 共用端口。Windows 上 8790 只要还有残留连接（包括 TIME_WAIT），服务就绑定不上；systemd 会每 30 秒重试一次，大约 2 分钟后残留连接清掉就能起来。
- **worker**：在「设置」→「GPU 转写」里填地址（`http://192.168.1.50:8790`）和令牌，点「测试」：连得上、令牌对就通过，不发音频。设置是 `asr_url`，令牌存在 `secrets/asr-token`。机房的「转写」灯显示上一期实际用的是 GPU 还是退回了 CPU。切窗口、时间偏移和繁简转换仍在 worker 里做，每个约 15 分钟的窗口发一次请求。
- **回退**：显卡机关机、连不上或中途出错时，这一期改用本地 CPU 转写。`TRANSCRIBE` 阶段详情里的 `device` 记录实际用的是哪一个，回退时会附带 `remote_failed`。

## Notion 批注推送

专题批注由 Notion integration webhook 通知：worker 下一次 tick 启动独立的 `topic-hook` 线程，循环处理到队列为空，定点读被批注的块或页本身的讨论，不调模型；停笔 10 分钟后沿用原来的重新综合和回复。每 6 小时整页逐块读一次、综合前整页读一次的兜底保留，覆盖漏事件和已解决、已删除的讨论。事件和兜底是两条线程，按专题互斥；某个专题正在整页读时，先处理其他专题。频道页只处理已定稿频道中课代表写的每期页（不是已登记），不定期整页扫描；只靠推送定点读，用户在原串再写一句可重新读到。停笔 10 分钟，整页未处理请求合成一次「批注」模型调用，只给文字和本节原话。已答、没改有本节原话时按模型选行附原话与时刻链接立即回，没有节时只回模型的话，程序毛病记进共用待修清单；已改只改受支持的文字块，等重新交付读回才在原串回。改正只在记下时的同一运行、同一模板有效，同版重印保留；换模板和换运行一样不带过去，成功交付读回时作废并在原串说明。代价是原文没变的段落，换模板后改正也带不过去。讨论串运行不变、模板变化时，先按记下的旧模板构建未套改正的计划并核对旧位置，只在旧版、新版本节里该原文各唯一一处时重新定位；同一批两串及以上汇合到一块，只要有换模板迁移来的串，全部不能已改。同运行、同模板的串仍只核对原位置；找不到时 plan_text 留空、不记原稿、不更新位置。模型冻结与应用时的运行或模板不同，已改转成固定作废说明，不写改正、不更新位置。交付完成事务仍同时记录 notion_synced_run 和 notion_synced_template；每批 checkpoint 的块映射另将实际交付运行和模板随计划键一起提交，保留配对块同样更新，并在同一事务删掉该块的旧键行，一个块只留一行映射；升级前遗留的同一块多行按没有计划键处理。扫描用提供计划键的同一映射行的版本，父块定位用父块那行；仅旧映射两列都为空时退回 sources 的版本，模板仍为空再退回当前设置。交付读回后未能带过去的改正作废、删除，在原串说明；已回「已改」的同样补一次作废通知。发送结果未知时先等 10 分钟，再按评论列表对账，依赖 Notion 届时能列出已成功的评论。模型失败或合同修一次仍失败就等新点名，不自动重调。偏好和重做不做，详见 [design §R3-12](design.md)。每批只有遇到未归档专题、行页或上述范围的频道页事件才查询一次 `/users/me`（单次尝试、5 秒超时），身份不跨批缓存；包括传输异常在内的查询失败仍定点读。自己的 bot 事件不读评论、块或页面；全是范围外频道页、未知页或归档专题时不查询身份。

| 盒子 | 推送域名 | 订阅地址 |
|---|---|---|
| devbox | `notion-hook-dev.example.com` | `https://notion-hook-dev.example.com/api/notion/webhook` |
| prodbox | `notion-hook.example.com` | `https://notion-hook.example.com/api/notion/webhook` |

每台盒子的 integration、验证令牌和隧道 token 相互独立。先在设置 → Notion填写对应的完整推送地址（设置项 `notion_hook_url`，默认空），也可以在选好的盒子里运行：

```bash
lzc-cli docker -- exec cloudlazycatappassetizer-web-1 python -m assetizer.cli set notion_hook_url https://notion-hook-dev.example.com/api/notion/webhook
```

隧道由编排者在 Cloudflare 建为远程管理的 Tunnel，DNS 指向该 tunnel。拓扑与 ingress 规则：

```text
Notion → https://notion-hook-dev.example.com/api/notion/webhook
       → Cloudflare Tunnel → hook-tunnel → http://web:8770
```

```yaml
ingress:
  - hostname: notion-hook-dev.example.com
    path: ^/api/notion/webhook$
    service: http://web:8770
    originRequest:
      httpHostHeader: notion-hook
  - service: http_status:404
```

入口不加 Access 登录或 challenge。Host 改为 `notion-hook` 后，应用 guard 只放行这一个精确路径的 POST，其余全部 404。隧道的路由、DNS 和令牌不在包里配置。

**隧道 token 由本人放置**，和 Notion token 的放法相同：在设置 → Notion「隧道 token」点「放」或「换」，粘贴后保存（只认得出远程管理隧道 token 的格式，不向 Cloudflare 核对）；也可以在自己的终端里执行下面这条，粘贴 token，回车后按 Ctrl-D。文件在持久卷里，不用重装；监督脚本 5 秒内就会看见新放的 token。cloudflared 运行中 token 被换掉或删掉时，同样 5 秒内发现（只比较校验和，不把 token 放进变量），让 cloudflared 正常退出，换了就立即用新 token 重启，删了就停着等。

```bash
lzc-cli docker -- exec -i cloudlazycatappassetizer-web-1 sh -c 'umask 077; mkdir -p /lzcapp/var/assetizer/hook-tunnel; cat > /lzcapp/var/assetizer/hook-tunnel/tunnel-token'
```

隧道 token 单独放在 `/lzcapp/var/assetizer/hook-tunnel/tunnel-token`，`hook-tunnel` 只绑定该目录到 `/ingress`，读 `/ingress/tunnel-token`，看不到应用数据库或其他凭据。web 和 doctor 只 stat 原路径，不读内容。旧的 secrets 路径不再使用，尚未放过 token，无须迁移。

`hook-tunnel` 没有 HTTP(S)_PROXY；文件不存在或为空时不连接 Cloudflare。有 token 才用 `--token-file` 启动 cloudflared，退出后隔 30 秒再拉起。运行时镜像内置 x86_64 的 cloudflared **2026.10.4**，打包时下载并校验 SHA256 `ae5e8add7884b0531d66bbca4570d1de491ff6da51e4ebe7eb160cdf241d9e42`，运行时不下载。不以 `/ready` 做服务 healthcheck，隧道出问题不会拦住整个应用；设置 → Notion状态才去查 `http://hook-tunnel:20241/ready`（1 秒超时），没文件是 off，200 是 up，其余是 down。

在 Notion integration 里：

1. 开启「读取评论」能力，将 integration 加进测试专题所在页的 Connections（生产库不用于试验）。要发送回复仍须「插入评论」。
2. 在 **Webhooks** 页签创建订阅，填该盒子的完整订阅地址，勾选 `comment.created`、`comment.updated`、`comment.deleted`。
3. Notion 发来一次验证请求。盒子没有验证令牌时自动接收第一个；设置 → Notion点「复制验证令牌」，把它粘回 Notion 的 **Verify** 框，完成验证。Notion 点 Verify 后不会主动再发通知；在已连接 integration 的测试页写一条批注，收到首个签名有效的事件后状态才会变成「已通」。只有点击复制时调用的 `/api/notion/hook/token` 返回令牌，其余状态、doctor、日志不显示值。
4. 有令牌以后换订阅或需要 Notion 重新发令牌时，先在设置 → Notion点「重新接收」，再回 Notion 点 **Resend token**。窗口 15 分钟内只收一次，收下即关闭；相同令牌直接确认、不记 warn、不关闭窗口；不同令牌过期或未打开时不换，warn 事件会说明操作。普通重试、隧道重启不需要重新接收。

验证令牌保存在 `secrets/notion-hook-verification`，0600、临时文件原子替换，不进 settings 和日志；收到时间另存 settings。换令牌后清空「首次有效签名」时间、rejected 拒收计数和 rejected_at 最近拒收时间，之后第一次有效签名说明 Notion 已开始投递。接收锁最多等 5 秒，独立数据库连接获取写锁也最多等 5 秒，超时回 503 由 Notion 重投，持久化成功才回 200；其他生产数据库连接仍等 120 秒。签名按收到的原始字节校验，请求全进程限 120 次/分钟，最大 64 KB。状态的 total 统计验签成功的事件请求（包括重复和非评论类型），rejected 统计签名拒绝，rejected_at 为最近一次拒收的 ISO 时间（没有时为 null），界面只显示 24 小时内的拒收；相同事件 id 只处理一次。元数据队列 `notion_events` 随日志 retention 清理 30 天前的行，专题处理错误记终态和不含原始内容的 warn，靠整页兜底。频道页读的 429、5xx、网络或超时错误不记 done，tries/retry_at 保存在事件队列，依次退避 1 分钟、5 分钟、30 分钟、2 小时、6 小时，第五次重试还失败才记终态和 warn，同次处理同一来源只记一条，带期次标题和事件数；尚未到期的事件不占处理线程。其他频道页错误照旧，终态后可在原串再写一句重新读到。

`python -m assetizer.cli asks --days 30` 列专题与频道共用的用量和待修，频道用量按 source 分组；频道待修带 `source_id` 和期次标题。`python -m assetizer.cli logs` 导出同样的数据，周一日报和设置里的批注数也按两者合计。修好用 `python -m assetizer.cli asks --fixed N --version V`，既有 `_tell_fixed` 会在原串回「〔课代表〕已修 #N · V」。频道调用的完整用量回执记在 `issue-<source_id>` 下，不计入采用运行；「批注」分工的额度和登录不挡生产、补录。模型输出和断点在数据目录 `issue-asks/<批次>/`，DONE 后删除、FAILED 保留；APPLY 的临时数据库错误下个 tick 只重做入库，不重新调模型；讨论串位置及原稿保存在 issue_positions，回复认领和完成均核 issue_replies.version，交付期间作废不会吞掉新说明；预览和失败交付不记改正错位 warn，成功读回作废时才按期次合记；`issue_state.failed_at` 只拦自动模型重调，不影响新推送读取。

`python -m assetizer.cli doctor` 的 `notion_hook` 一项列出验证令牌和隧道 token 文件是否存在、最近事件时间、签名拒绝次数，不输出凭据值。
