# 当前架构

课代表把长期关注的投资 / 金融长视频，按频道的学习目标拆成知识单元，生成文章、截图、视频片段、字幕和 Notion 页面。本页描述**现在的**系统结构；长期决定与理由见 [design.md](design.md)，频道页见 [channel-page.md](channel-page.md)。

## 进程与数据

两个进程共用一个数据目录（`ASSETIZER_HOME`）：

| 进程 | 入口 | 职责 |
|---|---|---|
| web | `python -m assetizer.cli web` → `src/assetizer/web/app.py`（FastAPI） | 界面与 API；不跑生产流水线，但学习目标的编译（后台线程）和起草在这里调用模型，试跑入队时也直接派发第一条；机房的 agy 网页登录（`providers/agy_login.py`，伪终端里的 agy 登录界面）、手动读额度、频道的立即扫描（后台线程）也在这里 |
| worker | `python -m assetizer.cli worker` → `pipeline/runner.py` 的 `Worker` | 生产、交付、试跑与补录派发、自动收录、健康探测、读 agy 额度（每 15 分钟）、保留期清理、调试样片的低清版（`playback.py`）、通知 |

数据目录里：`assetizer.db`（SQLite，WAL）、`runs/<run_id>/`（每次运行的产物与检查点）、`media/`（原始视频与工作文件）、`playback/`（调试样片的 360p 低清版，供频道页原片栏播放，滚动释放）、`packages/`（发布素材包，每期一份，按最后一次跑到 PACKAGE 的运行重建）、`compile/`（编译时的模型答复）、`secrets/`（凭据文件，见下）、`run/`（worker 心跳与正常退出标记）。

凭据只从环境变量 `ASSETIZER_<NAME>` 或 `secrets/<name>` 读取（`config.secret`）：`notion-token`、CookieCloud、Telegram 等。仓库里不放任何凭据。

## 生产流水线

`pipeline/runner.py` 的 `STAGES` 依次执行（第三列为是否阻塞：阻塞阶段失败则本期失败，非阻塞阶段失败记 `WARN` 后继续）：

```
RESOLVE · PLAYBOOK · ACQUIRE · CHUNK · UNDERSTAND          阻塞：取元数据、选规程、下载、分段、多模态理解
TRANSCRIBE（从 MERGE 起在后台跑，BOUNDARY 前完成）          非阻塞：whisper 转写
MERGE · KNOWLEDGE · ARTICLE                                 阻塞：合并、知识单元、文章
SELECT · SCREENSHOTS · BOUNDARY · CLIPS · SUBTITLES · MEDIA_QA · REVIEW_CHECK   非阻塞：选片、截图、边界校准、切片、字幕、媒体自检、校注预审
COMPOSE · PACKAGE                                           阻塞：组装页面与素材包
CLEANUP                                                     非阻塞
```

- 每个阶段写检查点；worker 重启（升级、崩溃）后从断点继续，进行中的模型调用会重来。
- 运行状态：`QUEUED → RUNNING → COMPLETE | COMPLETE_PARTIAL | FAILED | CANCELLED`（例如自动收录的视频短于最短时长），或 `WAITING`（等登录、等网络、等直播结束、等磁盘等，原因在 `wait_reason`；YouTube 登录、磁盘、直播、网络这几类每 30 分钟重新排队一次，模型登录恢复后整体继续）。
- 同时生产几期由设置 `worker_slots` 决定（1–16，默认 1），同时下载几个由 `download_slots` 决定（1–4，默认 2）；都在机房的「并行」卡片里改。盒子级设置的读法与范围集中在 `knobs.py`：存在 `settings` 表里、用到时才读，所以改了不用重启；超出范围的值按最近的边界读。
- 模型调用经 `providers/agy.py`（agy CLI）；结构化输出合同在 `contracts/` 与 `llm.py`。

## 频道生命周期

```
发现中（没有规程，用通用规程生产）
  └─ 写学习目标 → 编译出规程草稿 → 调试中（TUNING）
        试跑样片 · 采用 / 钉住 · 对比 · 改学习目标再编译
        └─ 定稿单 → 已定稿（LOCKED）：规程 ACTIVE，可开自动收录
              └─ 回到调试期：暂停收录，新的一期等重新定稿后才写 Notion
```

- 编译（`compiler.py`）：学习目标切成子句，模型产出规则（含短名）、频道字段、文章写法、子句去处与冲突，再由审计调用给规则打分；结果存为 `NEEDS_CONFIRMATION` 的规程版本。
- 调试与定稿逻辑在 `tuning.py`，界面在频道页。
- 自动收录（`ingest.py`）：对开启了收录的频道（定稿时开启，回到调试期关闭）每 30 分钟查一次新视频，只收开启之后发布、且不短于最短时长的视频。频道用 yt-dlp 带登录态平铺列出视频与直播 tab（最短时长低于 3 分钟时加 Shorts；Shorts 没有日期，之前扫描没列出过的才算新），公开 RSS 只在列表读不到时兜底；每次扫描记下会员视频收不收得进来（`members`，见 [design.md](design.md) §R6-6）。worker 每轮开始时记下时间（设置 `ingest_round`），页面的「下次」由它推算。每次扫描的结果（发现、新收、已在、已登记、太短、出错、会员视频）留最近 10 次在频道的收录设置里；看板的「立即扫描」做同样的一次，不改变 worker 的轮次。一个频道扫描出错不影响其他频道；「立即扫描」和 worker 同时收到同一期时，后到的一方算作「已在」。
- 补录（`ingest.py` 的 `enqueue_backfill` / `dispatch_backfill`）：已定稿频道在看板里按日期挑旧视频（`/backfill/candidates?since=&until=`，web 在后台用 yt-dlp 列到够深，最多 1000 期，留 10 分钟），或用「贴链接」补一条列表里看不到的不公开视频（`/backfill/link`，先读视频确认是本频道的），进 `backfill_queue`；worker 只在没有别的排队、没有试跑等着、且额度够时一次派一期（优先级 -1，`runs.origin = 'backfill'`），规则见 [design.md](design.md) §R6-5。回到调试期时取消排队的补录；「取消排队」和回到调试期只取消还没派出的期，worker 先把要派的那一期标成已派出、再去查库，正在派的那一期照常付印。看板的「试一期」用 `/candidates`（频道最新的 15 期，留 5 分钟）找最新还没收的一期，按正常优先级立即付印。

## 交付到 Notion

- 每个频道有自己的目标库与属性映射（设置 `notion:<频道>`，`notion/mapping.py`）；没有频道目标的期写入课代表自己的数据库（设置 `notion_database_id`，没有时在 `notion_parent_page_id` 下自动创建）。
- worker 在一期生产完成后，或空闲时，逐期交付（`delivery.deliver_pending` → `deliver`）。**调试中的频道不交付**，样片等定稿时按定稿单写入。
- 交付前核对映射（属性类型、选项是否存在）：不一致时该期 `BLOCKED`，原因以 `NOTION_MAPPING` 开头，频道显示「交付暂停」，修好映射后自动继续。
- 写入用块映射加续传前读回对账（`notion_blocks` 表），崩溃后不会重复追加。
- 按视频查重依赖目标库里映射为「视频链接」的属性。

## 主要数据表

`channels`、`channel_prompts`（学习目标版本）、`playbooks`（规程版本）、`sources`（每一期：采用的运行 `current_run_id`、是否钉住 `adopt_pinned`、交付状态 `delivery`、题眼 `headline`）、`runs`（`origin`：manual / ingest / backfill / trial，2026-10-28 之前的为空）、`stages`、`backfill_queue`（补录）、`units`（知识单元）、`sections`、`assets`（截图、片段、海报）、`flags`（校注）、`samples`（调试样片）、`trial_queue`、`notion_blocks`、`alerts`、`decisions`、`events`、`settings`、`model_calls`、`speakers`、`run_votes`（旧的「喜欢 / 备注」，只保留数据）。表结构与迁移都在 `db.py`（启动时自动补列）。

## 前端

React 19 + Vite + TypeScript，源码在 `web/src`，构建产物写入 `src/assetizer/web/static`（不进 Git）。页面：刊（`Issues.tsx`）、对照视图（`Reader.tsx`）、频道（`Channel.tsx`：总览 `components/channel/Overview.tsx`、看板 `Board.tsx`、调试台与其余 `components/channel/`）、机房（`Engine.tsx`）。数字与小图表（数字块、额度条、按天的柱、出刊日历、横条、走势线）在 `components/Figures.tsx`，三页共用；agy 登录面板 `components/AgyLogin.tsx` 挂在 App 上，告警里的「登录 agy」打开它。全局样式与色板在 `styles.css`，频道页样式在 `channel.css`。

页面读的接口：导航角标与频道页的告警灯读轻量的 `/api/status`；刊读 `/api/overview`（全部期次、同时在印的每一期 `running`、`drain_seconds`）；机房读 `/api/engine`（10 秒）与 `/api/engine/stats`（60 秒）；频道总览读 `/api/channels`（每个频道带 `stats`），看板读 `/api/channels/{cid}/board`。

## 访问控制

web 只接受三种请求（`app.py` 的 `guard`）：Host 头是 localhost / 127.0.0.1 / [::1]（只看 Host 头，不看来源地址，懒猫上靠容器网络隔离）；懒猫网关转发且带登录用户头（`ASSETIZER_TRUST_LAZYCAT=1`）；配置了公共域名时带有效 Cloudflare Access JWT。POST 只收 JSON。

## 部署形态

当前生产在懒猫微服 prodbox（LPK：一个运行时镜像，web / worker 两个服务），开发机是 devbox；做法见 [operations.md](operations.md)。仓库里的 `deploy/`（OVH 的 systemd 单元与 `deploy.sh`）是迁移到懒猫之前的部署方式。
