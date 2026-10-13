# Assetizer · 资产刊

把长期关注的投资 / 金融长视频，按频道学习目标自动拆成 Knowledge Units，并生成结构化文章、截图、实际视频片段、字幕、Notion 多媒体知识页与发布素材包。用户只在频道调试期参与（试跑样片、改 Prompt、定稿）；定稿后每期自动写入 Notion，日常只在 Notion 阅读。

- 产品基线：Notion「PRD v0.2 · Post-Validation Baseline」与「Product Validation Closure」；人的介入点以修订「PRD v0.2 → v0.2.1」为准
- 设计：[docs/design.md](docs/design.md)（v0.2，含独立评审采纳结果 §R）、[docs/design-review.md](docs/design-review.md)
- 代码评审：[docs/code-review.md](docs/code-review.md)

## 日常使用

```bash
ssh -N -L 8770:127.0.0.1:8770 ovh-investment     # 建隧道（保持此终端）
open http://localhost:8770                       # 打开「资产刊」
```

- **刊**：首页。粘贴 YouTube 链接（⌘K）即入队；正在生产的一期显示为「印刷线」；每一期主入口是 Notion，也可以点进对照视图。
- **对照视图**（原阅读器）：左侧整期时间轴（知识单元区段、片段、截图、校注红点，悬停预览，点击跳转）；中间是文章，每个单元依次是视频 → 截图 → 正文 → 频道专属字段；右侧是校注（系统没把握的地方，仅供参考，不是待办）。`J / K` 切换单元，`n` 逐条看校注。调试频道时用它对照原视频。
- **频道**：长期关注频道的学习目标（原始 Prompt）、编译后的 Playbook 与逐条溯源；频道分「调试中 / 已定稿」，调试中的频道在这里定稿（同时开启自动收录）。
- **机房**：模型 / YouTube / Notion / 磁盘状态、生产记录（可重试）、日志。

## 需要人做的事（且只有这些）

| 情况 | 你会看到 | 怎么做 |
|---|---|---|
| agy 登录失效 | 首页红色告警，队列自动暂停 | `ssh -t ovh-investment /home/debian/.local/bin/agy` 按提示完成 Google 登录；恢复后队列自动继续 |
| YouTube 需要登录 | 首页红色告警，该期进入等待 | 推荐用 CookieCloud（服务器直接用你 Chrome 里的登录态）：「机房」→「YouTube 会话」→「配置 CookieCloud」，填 origin、Access Client ID / Secret、App Token，保存后点「测试连接」。密钥只写不读，手机上也能填。没配 CookieCloud 时，用远程 Chromium：服务器上运行 `~/video-jobs/start-browser.sh`，本机 `ssh -N -L 6080:127.0.0.1:6080 ovh-investment`，打开 http://localhost:6080/vnc.html 登录 YouTube。系统每 30 分钟自动重试 |
| 新频道待定稿 | 首页「待你拍板」、导航「频道」角标 | 在「频道」页的**调试台**：选 2–3 期样片 → 用某个规程版本试跑 → 点「对照视图」看结果与校注 → 改学习目标重新编译 → 用新版本重跑、点「对比两个版本」→ 满意后「定稿并开启自动收录」。样片在定稿前不写 Notion；定稿时交付每期样片「采用中」的那一版 |
| 想调整已定稿频道 | — | 调试台「回到调试期」：自动收录暂停、新的一期等重新定稿后才写 Notion，然后按上面的流程试跑和定稿 |

## 配置生产 Notion

每个长期关注的频道写进你自己的数据库（「知识领袖长期追踪库」），挂在该频道的上级主页下，按你已有的页面格式：

- 交易员汤姆 → 上级「交易员汤姆」，标题 `交易员汤姆-YYYYMMDD-题眼`，正文「读图学习导航」（截图放在推荐截图位置下，每个案例嵌入视频片段）
- 王海滨 → 上级「持续学习王海滨的财经闲谈」，标题为视频原标题，正文「章节 + 📎原话参考」

**属性映射在「频道」页 → Notion 交付里调整**：属性随 Notion 实时读取；在 Notion 新加的属性会出现在这里等你选择来源；改属性名自动跟随；删掉属性、改类型、选项不存在时，该频道交付暂停并在首页告警，改好后自动继续。正文格式随频道固定（代码模板），只能在这里切换。

当前两个频道指向**测试副本**「🧪 知识领袖长期追踪库（测试副本）」。切换到正式库：

```bash
ssh ovh-investment
printf '%s' 'ntn_xxx' > ~/assetizer/data/secrets/notion-token && chmod 600 ~/assetizer/data/secrets/notion-token   # 如需换 token
set -a; . ~/assetizer/env; set +a; cd ~/assetizer/app
A=~/assetizer/venv/bin/assetizer
$A notion-target trader-tom  0000000200008000a000000000000002 --parent 0000000500008000a000000000000005
$A notion-target wang-haibin 0000000200008000a000000000000002 --parent 0000000300008000a000000000000003
```

`notion-target` 按预设把属性名对到属性 id；`--clear` 让频道回到 Assetizer 自己的数据库。Integration 需要被分享到目标数据库（数据库右上角 ··· → Connections）。没有指定目标的来源仍写入 `notion_parent_page_id` 下的「投资内容资产 · Assetizer」数据库。

## 部署

```bash
./deploy/deploy.sh          # 本机构建前端 → rsync 到 ovh-investment:~/assetizer/app → pip install → 重启服务
```

SSH：服务器只收钥匙登录，端口 24729（本机 `~/.ssh/config` 的 `ovh-investment` 已配好，本文命令都走这个别名）。设置在服务器 `/etc/ssh/sshd_config.d/00-hardening.conf`，由 `../work/OVH-SSH-hardening.command` 写入；SSH 进不去时用 OVH 控制台的 KVM，以 `debian` 的密码登录。

服务（systemd --user，已开启 linger）：`assetizer-web`（127.0.0.1:8770）、`assetizer-worker`（串行生产）。
数据：`~/assetizer/data/`（`assetizer.db`、`media/`、`runs/`、`packages/`、`secrets/`）。

```bash
set -a; . ~/assetizer/env; set +a
assetizer status            # 队列与告警
assetizer doctor --agy      # 依赖与登录探测
assetizer submit <url>      # 命令行入队
journalctl --user -u assetizer-worker -f
```

## 设计要点

- **串行**：同一时间只有一期在生产（50 分钟分段时 agy 峰值约 7.6 GiB）。全片 ASR 在纯文本阶段后台并行。
- **时间合同**：模型只报告它在画面里看到的时间码，全局时间由代码换算（实测关键帧偏移）。
- **结构化输出**：大输出阶段把 Schema 写进 Prompt（Run-014 验证路径）并本地校验；小输出阶段用 `--json-schema`。内容为空或不合规 → 重新生成一次；只有无法解析时才做「纯格式」修复，修复不得编造。
- **校注**：系统对边界、条件、归属没把握时生成 Flag；一次纯文本预审调用对照 ASR 检查片段首尾与陈述（CAVA 类问题）。调试期在对照视图里逐条看；稳定期不是待办，写进 Notion 每期页面末尾的折叠「校注」区块。没有逐期审阅（design §R2）。
- **Notion**：每期一行数据库记录；块映射 + 续传前读回对账，崩溃不会重复追加；交付独立于生产。
- **小硬盘**：agy 每次调用的会话缓存即时清理；原始媒体按保留期清理；发布包用硬链接。
