# 当前架构

课代表把长期关注的投资 / 金融长视频，按频道的学习目标拆成知识单元，生成文章、截图、视频片段、字幕和 Notion 页面。本页描述**现在的**系统结构；长期决定与理由见 [design.md](design.md)，频道页见 [channel-page.md](channel-page.md)。

## 进程与数据

两个进程共用一个数据目录（`ASSETIZER_HOME`）：

| 进程 | 入口 | 职责 |
|---|---|---|
| web | `python -m assetizer.cli web` → `src/assetizer/web/app.py`（FastAPI） | 界面与 API；不跑生产流水线，但学习目标的编译（后台线程）和起草在这里调用模型，试跑入队时也直接派发第一条；机房的 agy 网页登录（`providers/agy_login.py`，伪终端里的 agy 登录界面）、手动读额度、频道的立即扫描（后台线程）也在这里 |
| worker | `python -m assetizer.cli worker` → `pipeline/runner.py` 的 `Worker` | 生产、交付、试跑与补录派发、自动收录、健康探测、读 agy 额度（每 15 分钟）、保留期清理（原始媒体；日志每天一次，见 [operations.md](operations.md)「日志与迭代」）、调试样片的低清版（`playback.py`）、通知 |

数据目录里：`assetizer.db`（SQLite，WAL）、`runs/<run_id>/`（每次运行的产物与检查点）、`media/`（原始视频与工作文件）、`playback/`（调试样片的 360p 低清版，供频道页原片栏播放，滚动释放）、`packages/`（发布素材包，每期一份，按最后一次跑到 PACKAGE 的运行重建）、`compile/`（编译时的模型答复）、`logs/`（yt-dlp 的原话，留 30 天）、`secrets/`（凭据文件，见下）、`run/`（worker 心跳与正常退出标记）。

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

- 规程带 `fidelity: transcript`（原话模式，[design §R7](design.md)）时：KNOWLEDGE 等转写做完，按转写划分单元；ARTICLE 从原话逐段改写，最后写开头和文末清单；BOUNDARY 不调模型，片段照单元用到的原话剪，并去掉长静音。
- 原话模式写正文前先去掉落在长静音里的转写（语音识别会在静音处凭空生成句子），重写检查会把被换掉的粗口、贬义词、称呼和写长了的段落退回。频道设置 `screenshots:<频道>` 为 `off` 时，该频道不生成截图。
- 每个阶段写检查点；worker 重启（升级、崩溃）后从断点继续，进行中的模型调用会重来。
- 运行状态：`QUEUED → RUNNING → COMPLETE | COMPLETE_PARTIAL | FAILED | CANCELLED`（自动收录的视频短于最短时长；调试台「清空」时停下的试跑；视频不可用、被「忽略」的），或 `WAITING`（等登录、等网络、等直播结束、等磁盘、等下载工具更新、YouTube 拒绝下载等，原因在 `wait_reason`；YouTube 登录、YouTube 拒绝下载、磁盘、直播、网络、下载工具过旧这几类每 30 分钟重新排队一次，模型登录恢复后整体继续）。
- 断网、YouTube 限流、下载超时、yt-dlp 读不出视频都是等待，不判这一期失败；非阻塞工序遇到这类暂时故障也让整期等待，不跳过。YouTube 回 HTTP 403 却不说原因时，等「YouTube 拒绝下载」：登录态和下载工具都可能，告警里两种修法都写。已在等待的期再遇到认不出的报错，沿用原来的等待。数据库锁超时或 worker 处理一期时出了意外，这一期回到排队、从断点续跑，和 worker 重启一样连续 5 次后判失败。
- CookieCloud 带着 YouTube 登录仍报私享或会员限定，或视频已删除，判「视频不可用」（`VIDEO_PRIVATE`、`VIDEO_MEMBERS`、`VIDEO_REMOVED`；地区限制之类是 `SOURCE_UNAVAILABLE`），刊上写明原因，可以「忽略」（运行转为 `CANCELLED`）。视频下载好以后，后面的工序只用本地文件，中途下架也照常做完。
- 调试台的「清空」除了取消排队的试跑，还停下这个频道正在跑或在等的试跑：web 记下 `runs.stop_at`，不在 worker 上的立即取消；在跑的由 worker 杀掉它的模型调用（`providers/agy.py` 的 `LIVE_RUNS`），在下一道工序前停下。停下的记 `CANCELLED` / `STOPPED`，已完成的工序保留，可以「重试」接着跑，不会被采用。
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
- 自动收录（`ingest.py`）：对开启了收录的频道（定稿时开启，回到调试期关闭）每 30 分钟查一次新视频，只收开启之后发布、且不短于最短时长（0–240 分钟）的视频；还读不到时长的（直播刚结束、首播还没开始）先等，24 小时内拿到时长再判；以前只被当成太短取消过的期，列表里再出现且时长够了就重新收。频道用 yt-dlp 带登录态平铺列出视频与直播 tab（最短时长低于 3 分钟时加 Shorts；Shorts 没有日期，之前扫描没列出过的才算新），公开 RSS 只在列表读不到时兜底；每次扫描记下会员视频收不收得进来（`members`，见 [design.md](design.md) §R6-6）。worker 每轮开始时记下时间（设置 `ingest_round`），页面的「下次」由它推算。每次扫描的结果（发现、新收、已在、已登记、太短、出错、会员视频）留最近 10 次在频道的收录设置里；看板的「立即扫描」做同样的一次，不改变 worker 的轮次。一个频道扫描出错不影响其他频道；「立即扫描」和 worker 同时收到同一期时，后到的一方算作「已在」。
- 补录（`ingest.py` 的 `enqueue_backfill` / `dispatch_backfill`）：已定稿频道在看板里按日期挑旧视频（`/backfill/candidates?since=&until=`，web 在后台用 yt-dlp 列到够深，最多 1000 期，留 10 分钟），或用「贴链接」补一条列表里看不到的不公开视频（`/backfill/link`，先读视频确认是本频道的），进 `backfill_queue`；worker 只在没有别的排队、没有试跑等着、且额度够时一次派一期（优先级 -1，`runs.origin = 'backfill'`），规则见 [design.md](design.md) §R6-5。回到调试期时取消排队的补录；「取消排队」和回到调试期只取消还没派出的期，worker 先把要派的那一期标成已派出、再去查库，正在派的那一期照常付印。看板的「试一期」用 `/candidates`（频道最新的 15 期，留 5 分钟）找最新还没收的一期，按正常优先级立即付印。

## 交付到 Notion

- 每个频道有自己的目标库与属性映射（设置 `notion:<频道>`，`notion/mapping.py`）；没有频道目标的期写入课代表自己的数据库（设置 `notion_database_id`，没有时在 `notion_parent_page_id` 下自动创建）。
- worker 在一期生产完成后，或空闲时，逐期交付（`delivery.deliver_pending` → `deliver`）。**调试中的频道不交付**，样片等定稿时按定稿单写入。
- 交付前核对映射（属性类型、选项是否存在）：不一致时该期 `BLOCKED`，原因以 `NOTION_MAPPING` 开头，频道显示「交付暂停」，修好映射后自动继续。
- 写入用块映射加续传前读回对账（`notion_blocks` 表），崩溃后不会重复追加。续传只删课代表自己建的块；属性只改仍是上次写入值的（上次写入的值记在设置 `notion_props:<视频>`），你在 Notion 里加的块、改过的属性留着。
- 按视频查重依赖目标库里映射为「视频链接」的属性，查询结果翻页读完。查到的页是课代表自己建的、又没被别的期认领（建页的回答丢了）时，认领下来接着续传。
- 换掉旧页时归档失败（页已删或已归档除外），本次交付失败、稍后重试，不新建第二页。
- 几种情况先不写、停下等你（`HELD`，原因在 `delivery_reason`，刊的「需要你」里各有一行）：
  - `PARTIAL`：当前版本是「部分完成」（非阻塞工序有一批没做完，缺片段、截图或校注）。不管从定稿、钉住还是重新交付过来都不写；重试这一版跑完整后照常交付（`adopt_completed` 解除）。
  - `MAYBE_HAVE`：补录进来的期，库里有这个频道没填视频链接的页（手写的）可能就是它。按日期起标题的频道（汤姆）比「内容所属 + 日期」，日期取 YouTube 的上传日、纽约当天和标题里的 MM/DD；用视频原标题的频道（王海滨）比标题。你选「照写」（另写一页）或「算已有」（登记为 `REGISTERED`）。补录列表里同样的比对标「可能已有」，默认不勾（列表的日期是近似值，只用标题里的 MM/DD）。
  - `YOURS_ON_PAGE`：要换掉旧页（重印、换成新版本、同一版内容变了要重建）之前，先看旧页上有没有别人做的事：加的段落、改过的课代表段落、改过的属性、评论（integration 有读评论权限时）。有就不动旧页，你选「留着旧页」（这一版不写进 Notion）或「换新页」（旧页进 Notion 回收站，30 天内能找回）。
- 映射按属性 id 记；在 Notion 删掉一个属性又建了同名同类型的，库里只有这一个、也没被别的映射用上时自动接上（`mapping.sync`），类型变了或已被占用仍要重新选。
- 频道的 Notion 设置带修订号（`rev`，保存和换库时加一，跟随 Notion 里的改名不加）。页面保存时带回读到的修订号，对不上返回 412，界面让你选「用我的覆盖」或「放弃我的」。
- 已写进 Notion 的视频再贴一次，不重印：返回那一页（`kept`），要重印时带 `reprint` 再提交。

## 主要数据表

`channels`、`channel_prompts`（学习目标版本）、`playbooks`（规程版本）、`sources`（每一期：采用的运行 `current_run_id`、是否钉住 `adopt_pinned`、交付状态 `delivery`、题眼 `headline`）、`runs`（`origin`：manual / ingest / backfill / trial，2026-10-28 之前的为空；`stop_at`：「清空」要它停下）、`stages`、`backfill_queue`（补录）、`units`（知识单元）、`sections`、`assets`（截图、片段、海报）、`flags`（校注）、`samples`（调试样片）、`trial_queue`、`notion_blocks`、`alerts`（`notified_at`：推送过没有，非紧急的等 10 分钟）、`notify_outbox`（没发出去的 Telegram 通知）、`decisions`、`events`、`settings`、`model_calls`、`speakers`、`run_votes`（旧的「喜欢 / 备注」，只保留数据）。表结构与迁移都在 `db.py`（启动时自动补列）。

## 前端

React 19 + Vite + TypeScript，源码在 `web/src`，构建产物写入 `src/assetizer/web/static`（不进 Git）。页面：刊（`Issues.tsx`）、对照视图（`Reader.tsx`）、频道（`Channel.tsx`：总览 `components/channel/Overview.tsx`、看板 `Board.tsx`、调试台与其余 `components/channel/`）、机房（`Engine.tsx`）。数字与小图表（数字块、额度条、按天的柱、出刊日历、横条、走势线）在 `components/Figures.tsx`，三页共用；agy 登录面板 `components/AgyLogin.tsx` 挂在 App 上，告警里的「登录 agy」打开它。全局样式与色板在 `styles.css`，频道页样式在 `channel.css`。

页面读的接口：导航角标与频道页的告警灯读轻量的 `/api/status`；刊读 `/api/overview`（全部期次、同时在印的每一期 `running`、`drain_seconds`；期次和看板一样由 `_issues` 一次批量读出，不按期逐条查，2000 期约 15 条查询）；机房读 `/api/engine`（10 秒）与 `/api/engine/stats`（60 秒）；频道总览读 `/api/channels`（每个频道带 `stats`），看板读 `/api/channels/{cid}/board`。轮询的页面在切回、重新获得焦点或恢复联网时立即再读一次（`hooks.ts` 的 `useBack`）；调试台空闲时每 60 秒、有试跑在跑或排队时每 15 秒读一次。

## 访问控制

web 只接受三种请求（`app.py` 的 `guard`）：Host 头是 localhost / 127.0.0.1 / [::1]（只看 Host 头，不看来源地址，懒猫上靠容器网络隔离）；懒猫网关转发且带登录用户头（`ASSETIZER_TRUST_LAZYCAT=1`）；配置了公共域名时带有效 Cloudflare Access JWT。POST 只收 JSON。

## 部署形态

当前生产在懒猫微服 prodbox（LPK：一个运行时镜像，web / worker 两个服务），开发机是 devbox；做法见 [operations.md](operations.md)。仓库里的 `deploy/`（OVH 的 systemd 单元与 `deploy.sh`）是迁移到懒猫之前的部署方式。
