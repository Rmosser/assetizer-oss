# 运维：部署、升级与排障

课代表以懒猫 LPK 运行（包名 `cloud.lazycat.app.assetizer`，版本在 `package.yml`）。两台盒子：

| 盒子 | 用途 | 入口 |
|---|---|---|
| prodbox | 生产（日常使用，生产与交付都在这里跑） | https://assetizer.prodbox.heiyu.space |
| devbox | 开发机，验证安装包 | https://assetizer.devbox.heiyu.space |

两台各有独立的数据库和凭据。截至 2026-10-26，devbox 没有 Notion token，写不了 Notion。

## 发版

1. 改 `package.yml` 的 `version` 并提交（提交标题沿用「版本 x.y.z：一句话说明」）。
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

6. 验收：
   - `lzc-cli docker -- ps -a | grep assetizer`：app / web / worker 三个容器都是 healthy；
   - 版本：`lzc-cli docker -- exec cloudlazycatappassetizer-web-1 grep ^version /lzcapp/pkg/package.yml`；
   - worker 日志：`lzc-cli docker -- logs --tail 200 cloudlazycatappassetizer-worker-1`；
   - 打开入口，看刊、频道、机房三页。

   `lzc-cli app status` 的 `Installed` 只说明装上了；lzc-cli 2.0.9 的 `app log` 未实现。

**回滚**：重装上一个版本的 LPK。数据库迁移只补列（`db.py` 的 `_migrate`），旧版本能读新库。需要恢复数据时先停应用，再用备份替换 `assetizer.db` 并删掉旁边的 `-wal` / `-shm`。

## 容器里的命令行

```bash
lzc-cli docker -- exec cloudlazycatappassetizer-web-1 python -m assetizer.cli <命令>
```

常用：`status`（队列与最近的运行）、`doctor`（依赖与凭据文件是否在；加 `--agy` 才探测模型登录，会调用一次模型）、`cookies`（探测 CookieCloud）、`set <键> <值>`（机房卡片里能改的设置也可以这样写，键和范围见 `knobs.py`，例如 `set worker_slots 2`）、`submit <链接>`、`deliver <视频 id>`、`telegram-setup`、`notify-digest`。

队列由 worker 自动暂停和恢复（例如模型登录失效、读不了视频），不要手动设置 `queue_paused`；需要立即恢复时，用首页的「恢复队列」或机房的「手动恢复」，它们还会把等待中的运行重新排队、解除对应告警。agy 的登录告警例外：按钮不解除它，只在 agy 回答 `/usage` 时解除（恢复时会在后台问一次）；因 agy 登录暂停时页面只给「登录 agy」。在终端里登录之后，点机房额度卡的「刷新」，读到额度即解除告警、恢复队列。

## 凭据与登录

凭据只能由人放置，不进 Git，不出现在日志里。

- **Notion token**：`/lzcapp/var/assetizer/data/secrets/notion-token`。在自己的终端里执行下面这条，粘贴 token，回车后按 Ctrl-D；不用重启：

  ```bash
  lzc-cli docker -- exec -i cloudlazycatappassetizer-web-1 sh -c 'umask 077; cat > /lzcapp/var/assetizer/data/secrets/notion-token'
  ```

  integration 必须被加到目标库（或其上级页面）的 Connections 里，频道页「换库」才看得到它。
- **agy（模型）登录**：首页或机房告警「模型登录已失效，生产已暂停」时，点告警上的「登录 agy」：web 在伪终端里启动 agy 的登录界面并选 Google OAuth，页面给出授权链接；在 Google 登录后，把授权页给出的授权码（或整段回调地址）贴回面板。登录态和 agy 本身都在持久卷 `/lzcapp/var/assetizer/home` 里，web 与 worker 共用；确认登录以 `agy -p /usage` 能回答为准，成功后告警解除、因登录暂停的队列继续。链接 10 分钟内有效，关掉面板不中止，重新打开会接上。网页登录不成时，进 worker 容器运行 agy：

  ```bash
  lzc-cli docker -- exec -it cloudlazycatappassetizer-worker-1 /lzcapp/var/assetizer/home/.local/bin/agy
  ```

  网页只处理「没登录」的情况；agy 已登录时不会退出重登，换 Google 账号仍用上面的命令行（在 agy 里 `/logout` 再登录）。
- **agy 额度**：`agy -p /usage --output-format json` 由 agy 自己回答，不开对话、不耗额度。worker 在 agy 在用时每 15 分钟读一次（设置 `agy_quota`、一周走势 `agy_quota_log`），机房的「刷新」立即读一次。ChatGPT（magpie）的额度取自最近一次调用回执里的 `codex.rate_limits`（设置 `magpie_quota`）。

- **YouTube**：用 CookieCloud，在「机房」→「YouTube 会话」里按面板步骤配置（[cookiecloud-credentials-ui.md](cookiecloud-credentials-ui.md)）。

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

然后打开 http://127.0.0.1:18770 。到 prodbox 的 SSH 偶尔会断开，长时间使用时套一层自动重连。盒子上的规则（`/AGENTS.md`）：lzcapp 容器用 `lzc-docker` 管理；不要从 `/`、`/data`、`/lzcsys/data` 递归扫描；找数据目录前先读 `/lzcsys/data/README`。

## GPU 转写（局域网 4070 Ti）

whisper 转写可以交给局域网里的 Windows 显卡机（RTX 4070 Ti，`192.168.1.50`）。实测 59 分钟音频，GPU 用 51 秒，space 的 CPU 用 29 分钟；文字一致度约 95%，差异主要在语气词上。

- **显卡机**：跑在 WSL Debian 13（`debian-gpu`，镜像网络模式，直接用 192.168.1.50）里，不在 Windows 原生环境里跑。同一段 59 分钟音频，WSL 用 39 秒，Windows 原生用 50 秒。`scripts/gpu_asr/server.py` 放在 `~/voice-lab/asr`，环境是 `~/voice-lab/envs/asr`（faster-whisper、nvidia-cublas-cu12、nvidia-cudnn-cu12 9），模型在 `~/voice-lab/models/hf`，令牌在 `~/voice-lab/asr/token`，监听 8790 端口。服务用 systemd 管理（`scripts/gpu_asr/assetizer-asr.service` → `/etc/systemd/system/`），日志用 `journalctl -u assetizer-asr` 查看。模型空闲 10 分钟后自动卸载，释放显存。
- **常驻**：WSL 开了 systemd，`.wslconfig`（`%USERPROFILE%`）设了 `instanceIdleTimeout=-1` 和 `vmIdleTimeout=-1`，没有会话连着也不会关机；Windows 登录时，计划任务「WSL debian-gpu autostart」会启动发行版，服务随之起来。Windows 重启后如果没有人登录，WSL 不会启动，转写会退回 CPU。
- **端口坑**：镜像网络下，WSL 和 Windows 共用端口。Windows 上 8790 只要还有残留连接（包括 TIME_WAIT），服务就绑定不上；systemd 会每 30 秒重试一次，大约 2 分钟后残留连接清掉就能起来。
- **worker**：在机房的「GPU 转写」卡片里填地址（`http://192.168.1.50:8790`）和令牌，点「测试」：连得上、令牌对就通过，不发音频。设置是 `asr_url`，令牌存在 `secrets/asr-token`。状态灯「转写」显示上一期实际用的是 GPU 还是退回了 CPU。切窗口、时间偏移和繁简转换仍在 worker 里做，每个约 15 分钟的窗口发一次请求。
- **回退**：显卡机关机、连不上或中途出错时，这一期改用本地 CPU 转写。`TRANSCRIBE` 阶段详情里的 `device` 记录实际用的是哪一个，回退时会附带 `remote_failed`。

## OVH（迁移前的部署）

`deploy/deploy.sh` 与 `deploy/*.service` 是迁移到懒猫之前在 OVH 上的部署方式（systemd --user，经 SSH 隧道访问 127.0.0.1:8770，另有经 Cloudflare Access 的公网入口 `kan.example.com`，即 `ASSETIZER_PUBLIC_HOST`）。OVH 退役时一并下线那条 tunnel 和 Access 应用，并删掉 `app.py` 里公网入口的分支与远程浏览器接口。迁移方案见 [history/lazycat-lpk-plan.md](history/lazycat-lpk-plan.md)；OVH 退役后删除这些文件。
