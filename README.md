# Assetizer · 资产刊

把长期关注的投资 / 金融长视频，按频道学习目标自动拆成 Knowledge Units，并生成结构化文章、截图、实际视频片段、字幕、Notion 多媒体知识页与发布素材包。用户只负责最后阅读、看片与审阅（朱批）。

- 产品基线：Notion「PRD v0.2 · Post-Validation Baseline」与「Product Validation Closure」
- 设计：[docs/design.md](docs/design.md)（v0.2，含独立评审采纳结果 §R）、[docs/design-review.md](docs/design-review.md)
- 代码评审：[docs/code-review.md](docs/code-review.md)

## 日常使用

```bash
ssh -N -L 8770:127.0.0.1:8770 ovh-investment     # 建隧道（保持此终端）
open http://localhost:8770                       # 打开「资产刊」
```

- **刊**：首页。粘贴 YouTube 链接（⌘K）即入队；正在生产的一期显示为「印刷线」；目录里每一期点进去就是阅读器。
- **阅读器**：左侧整期时间轴（知识单元区段、片段、截图、朱批红点，悬停预览，点击跳转）；中间是文章，每个单元依次是视频 → 截图 → 正文 → 频道专属字段；右侧是朱批（需要你判断的地方）。`J / K` 切换单元。
- **频道**：长期关注频道的学习目标（原始 Prompt）、编译后的 Playbook 与逐条溯源；待确认的 Playbook 在这里一键确认。
- **机房**：模型 / YouTube / Notion / 磁盘状态、生产记录（可重试）、日志。

## 需要人做的事（且只有这些）

| 情况 | 你会看到 | 怎么做 |
|---|---|---|
| agy 登录失效 | 首页红色告警，队列自动暂停 | `ssh -t ovh-investment /home/debian/.local/bin/agy` 按提示完成 Google 登录；恢复后队列自动继续 |
| YouTube 需要登录 | 首页红色告警，该期进入等待 | 服务器上运行 `~/video-jobs/start-browser.sh`，本机 `ssh -N -L 6080:127.0.0.1:6080 ovh-investment`，打开 http://localhost:6080/vnc.html 在远程 Chromium 中登录 YouTube；系统每 30 分钟自动重试 |
| 新频道 Playbook 待确认 | 首页「待你拍板」 | 在「频道」页查看规则与冲突说明，点「确认并启用」 |
| 朱批 | 首页「朱批 N 处」 | 在阅读器右栏逐条：确认无误 / 已修正 / 忽略 |

## 配置生产 Notion

当前使用**临时测试 token**，交付到项目页下的「🧪 Assetizer 测试交付（临时 token，可删除）」。切换生产：

```bash
ssh ovh-investment
printf '%s' 'ntn_xxx' > ~/assetizer/data/secrets/notion-token && chmod 600 ~/assetizer/data/secrets/notion-token
set -a; . ~/assetizer/env; set +a; cd ~/assetizer/app
assetizer set notion_parent_page_id '"<目标父页面 id>"'    # 系统会在其下创建「投资内容资产」数据库
assetizer set notion_database_id 'null'                   # 清掉测试数据库，下一次交付自动新建
```

Integration 需要被分享到目标父页面（Notion 页面右上角 ··· → Connections）。

## 部署

```bash
./deploy/deploy.sh          # 本机构建前端 → rsync 到 ovh-investment:~/assetizer/app → pip install → 重启服务
```

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
- **朱批**：系统对边界、条件、归属没把握时生成 Flag；一次纯文本预审调用对照 ASR 检查片段首尾与陈述（CAVA 类问题）。
- **Notion**：每期一行数据库记录；块映射 + 续传前读回对账，崩溃不会重复追加；交付独立于生产。
- **小硬盘**：agy 每次调用的会话缓存即时清理；原始媒体按保留期清理；发布包用硬链接。
