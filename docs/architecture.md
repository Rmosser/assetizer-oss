# 当前架构

课代表把长期关注的投资 / 金融长视频，按频道的学习目标拆成知识单元，生成文章、截图、视频片段、字幕和 Notion 页面。本页描述**现在的**系统结构；长期决定与理由见 [design.md](design.md)，频道页见 [channel-page.md](channel-page.md)。

## 进程与数据

两个进程共用一个数据目录（`ASSETIZER_HOME`）：

| 进程 | 入口 | 职责 |
|---|---|---|
| web | `python -m assetizer.cli web` → `src/assetizer/web/app.py`（FastAPI） | 界面与 API；不跑生产流水线，但学习目标的编译（后台线程）和起草在这里调用模型，试跑入队时也直接派发第一条；机房的 agy 网页登录（`providers/agy_login.py`，伪终端里的 agy 登录界面）、手动读额度、频道的立即扫描（后台线程）、新专题读链接里的视频列表（yt-dlp，不下载）也在这里；专题的接口不调模型，只排进 worker |
| worker | `python -m assetizer.cli worker` → `pipeline/runner.py` 的 `Worker` | 生产、交付、试跑与补录派发、自动收录、健康探测、读 agy 额度（每 15 分钟）、保留期清理（原始媒体；日志每天一次，见 [operations.md](operations.md)「日志与迭代」）、调试样片与专题视频的低清版（`playback.py`）、专题的读与综合（`topics/run.py`）、通知 |

数据目录里：`assetizer.db`（SQLite，WAL）、`runs/<run_id>/`（每次运行的产物与检查点）、`media/`（原始视频与工作文件）、`playback/`（调试样片的 360p 低清版，供频道页原片栏播放，滚动释放）、`packages/`（发布素材包，每期一份，按最后一次跑到 PACKAGE 的运行重建）、`compile/`（编译时的模型答复）、`logs/`（yt-dlp 的原话，留 30 天）、`cache/magpie-media/`（magpie 的抽帧缓存，目前不清理）、`health/`（模型自检的临时目录）、`bench/`（基准的报告与媒体）、`topics/`（专题：`media/<视频>/` 原片与 `drafts/<视频>/` 底稿按视频存，`<专题>/r<修订>/<视频>/` 是每次读的计划、回看与笔记，`<专题>/synth/` 是综合的模型答复）、`secrets/`（凭据文件，见下）、`run/`（worker 心跳与正常退出标记）。

凭据只从环境变量 `ASSETIZER_<NAME>` 或 `secrets/<name>` 读取（`config.secret`）：`notion-token`、CookieCloud、Telegram、`magpie-api-key`、`tuzi-api-key` 等。仓库里不放任何凭据。

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
- 调试台的「清空」除了取消排队的试跑，还停下这个频道正在跑或在等的试跑：web 记下 `runs.stop_at`，不在 worker 上的立即取消；在跑的由 worker 杀掉它的 agy 和 whisper 子进程（`providers/agy.py` 的 `LIVE_RUNS`），magpie 和兔子的 HTTP 调用杀不掉、要等它结束，然后在下一道工序前停下。停下的记 `CANCELLED` / `STOPPED`，已完成的工序保留，可以「重试」接着跑，不会被采用。
- 同时生产几期由设置 `worker_slots` 决定（1–16，默认 1），同时下载几个由 `download_slots` 决定（1–4，默认 2）；都在「设置」→「并行」里改。盒子级设置的读法与范围集中在 `knobs.py`：存在 `settings` 表里、用到时才读，所以改了不用重启；超出范围的值按最近的边界读。
- 模型调用经 `llm.py` 和 `providers.for_stage(stage)`：先查设置 `stage_providers`（键是阶段名的第一段），再查 `llm_provider`，默认 agy。共用提示词和校验：agy 是 CLI，用 Google 订阅额度；magpie 是 ChatGPT、Claude、Grok 订阅的网关，模型和额度自动读取（`providers/magpie_state.py`），以抽帧加转写代替视频；兔子（tuzi）是按量网关，只为还在用它的盒子保留，机房不提供。学习目标的编译和起草只走默认 provider。结构化输出合同在 `contracts/` 与 `llm.py`。调用出错按错误类型重试（设置 `retry_counts`，design §R6-10）：`llm.py` 只重新调用失败的那一次，运行的 `retry` 字段写着正在重试什么，结果记进 `retries` 表。

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
- 补录（`ingest.py` 的 `enqueue_backfill` / `dispatch_backfill`）：已定稿频道在看板里按日期挑旧视频（`/backfill/candidates?since=&until=`，web 在后台用 yt-dlp 列到够深，最多 1000 期，留 10 分钟），或用「贴链接」补一条列表里看不到的不公开视频（`/backfill/link`，先读视频确认是本频道的），进 `backfill_queue`；worker 只在有空闲路数、没有别的排队、没有试跑等着、且额度够时派（优先级 -1，`runs.origin = 'backfill'`）。`backfill_pace()` 算现在几路：平时一路；`backfill_hours`（东八区整点，机房里涂）涂到的钟点和 `backfill_burst`（`{"until"}`，`null` 是不限时）生效期间 `backfill_fast_slots` 路（默认 8，不超过 `worker_slots`），到点后正在跑的跑完，降到一路再派新的，规则见 [design.md](design.md) §R6-5。`backfill_view()` 给机房和看板同一份：排队多少、在跑几期、几路、到几点、什么挡着（`backfill_stop()`）。回到调试期时取消排队的补录；「取消排队」和回到调试期只取消还没派出的期，worker 先把要派的那一期标成已派出、再去查库，正在派的那一期照常付印。看板的「试一期」用 `/candidates?limit=5`（接口上限 15，留 5 分钟）找最新还没收、不太短的一期，按正常优先级立即付印。

## 专题

专题（[topics.md](topics.md)、[design §R8](design.md)）不走上面的流水线、不建运行：每期在 `topic_videos` 里有自己的状态与步骤（`ACQUIRE → DRAFT → PLAN → LOOK → SECOND → WRITE`，有没把握的再 `LOOK2 → SECOND2 → REVISE`），读完再综合。worker 在派完生产和试跑、补录之前调 `topics.run.dispatch`：先派要做的综合，再在 `topic_slots` 之内派下一期；一期占一个并行槽（`Worker.active` 里的 `topic:<专题>:<视频>`），不占运行。分工用到的订阅低于专题的额度留底（`run.held`）时不派新的；槽满又有运行排队时，在读的一期在下一步之前让出槽（`run.crowded`）；同一个视频的底稿同时只有一个专题在录（`run.drafting`）。四道工序的模型由 `knobs.topic_routes` 决定，`providers.for_stage` 遇到 `draft`、`look`、`second`、`topic` 开头的工序名就走它，不看 `stage_providers`。

## 交付到 Notion

- 每个频道有自己的目标库与属性映射（设置 `notion:<频道>`，`notion/mapping.py`）；没有频道目标的期写入课代表自己的数据库（设置 `notion_database_id`，没有时在 `notion_parent_page_id` 下自动创建）。
- worker 在一期生产完成后，或空闲时，逐期交付（`delivery.deliver_pending` → `deliver`）。**调试中的频道不交付**，样片等定稿时按定稿单写入。
- 交付前核对映射（属性类型、选项是否存在）：不一致时该期 `BLOCKED`，原因以 `NOTION_MAPPING` 开头，频道显示「交付暂停」，修好映射后自动继续。
- 新频道的页面图标默认是频道头像（`icon: "channel"`，`channel_icons.py`），卡片上可以改：用 yt-dlp 取频道最大的方形头像地址，存在设置 `channel_avatar:<频道>`，worker 在后台线程里每周刷新（`channel_icons.refresh`），`sync` 和预览只读设置，不问 YouTube。频道总览、页头和图标卡片也显示这张头像。新页面建立时带上；旧页面由 worker 每分钟改最多 20 个，只改图标，`sources.notion_icon` 记着每页用的地址，地址变了会重写。
- 写入用块映射加续传前读回对账（`notion_blocks` 表），崩溃后不会重复追加。续传只删课代表自己建的块；属性只改仍是上次写入值的（上次写入的值记在设置 `notion_props:<视频>`），你在 Notion 里加的块、改过的属性留着。
- 按视频查重依赖目标库里映射为「视频链接」的属性。批量查询（`registered_many`、`hand_written`）翻页读完；交付时查单个视频（`registered`）只读第一页（10 条）。查到的页是课代表自己建的、又没被别的期认领（建页的回答丢了）时，认领下来接着续传。
- 换掉旧页时归档失败（页已删或已归档除外），本次交付失败、稍后重试，不新建第二页。例外：同一版内容变了要重建时，归档失败被忽略、照样建新页，可能留下第二页（`delivery.py` 的 `changed or orphaned` 分支）。
- 几种情况先不写、停下等你（`HELD`，原因在 `delivery_reason`，刊的「需要你」里各有一行）：
  - `PARTIAL`：当前版本是「部分完成」（`COMPLETE_PARTIAL`：一道非阻塞工序整体记了 `WARN`）。不管从定稿、钉住还是重新交付过来都不写；重试这一版跑完整后照常交付（`adopt_completed` 解除）。工序内部的局部失败不算：部分片段剪辑失败、截图失败、预审某批失败时仍是 `COMPLETE`，照常交付，片段和截图的失败记成校注。
  - `MAYBE_HAVE`：补录进来的期，库里有这个频道没填视频链接的页（手写的）可能就是它。按日期起标题的频道（汤姆）比「内容所属 + 日期」，日期取 YouTube 的上传日、纽约当天和标题里的 MM/DD；用视频原标题的频道（王海滨）比标题。你选「照写」（另写一页）或「算已有」（登记为 `REGISTERED`）。补录列表里同样的比对标「可能已有」，默认不勾（列表的日期是近似值，只用标题里的 MM/DD）。
  - `YOURS_ON_PAGE`：要换掉旧页（重印、换成新版本、同一版内容变了要重建）之前，先看旧页上有没有别人做的事：加的段落、改过的课代表段落、改过的属性、评论（integration 有读评论权限时）。有就不动旧页，你选「留着旧页」（这一版不写进 Notion）或「换新页」（旧页进 Notion 回收站，30 天内能找回）。
- 映射按属性 id 记；在 Notion 删掉一个属性又建了同名同类型的，库里只有这一个、也没被别的映射用上时自动接上（`mapping.sync`），类型变了或已被占用仍要重新选。
- 频道的 Notion 设置带修订号（`rev`，保存和换库时加一，跟随 Notion 里的改名不加）。页面保存时带回读到的修订号，对不上返回 412，界面让你选「用我的覆盖」或「放弃我的」。
- 已写进 Notion 的视频再贴一次，不重印：返回那一页（`kept`），要重印时带 `reprint` 再提交。

## 主要数据表

`channels`、`channel_prompts`（学习目标版本）、`playbooks`（规程版本）、`sources`（每一期：采用的运行 `current_run_id`、是否钉住 `adopt_pinned`、交付状态 `delivery`、题眼 `headline`）、`runs`（`origin`：manual / ingest / backfill / trial / lock（定稿单排入或等着的），2026-10-28 之前的为空；`stop_at`：「清空」要它停下）、`stages`、`backfill_queue`（补录）、`units`（知识单元）、`sections`、`assets`（截图、片段、海报）、`flags`（校注）、`samples`（调试样片）、`trial_queue`、`notion_blocks`、`alerts`（`notified_at`：推送过没有，非紧急的等 10 分钟）、`notify_outbox`（没发出去的 Telegram 通知）、`decisions`、`events`、`settings`、`model_calls`、`speakers`、`run_votes`（旧的「喜欢 / 备注」，只保留数据）；专题的 `topics`（问题、修订号、综合）、`videos`（专题的视频，原片与底稿按视频，几个专题共用）、`topic_videos`（一个专题里的一期：状态、步骤、判断、是否算进回答）。表结构与迁移都在 `db.py`（启动时自动补列）。

## 前端

React 19 + Vite + TypeScript，源码在 `web/src`，构建产物写入 `src/assetizer/web/static`（不进 Git）。页面：刊（`Issues.tsx`）、对照视图（`Reader.tsx`）、频道（`Channel.tsx`：总览 `components/channel/Overview.tsx`、看板 `Board.tsx`、调试台与其余 `components/channel/`）、专题（`Topics.tsx` 总览、`Topic.tsx` 一个专题、`TopicVideo.tsx` 一期，组件在 `components/topic/`，样式在 `topic.css`）、机房（`Engine.tsx`）、设置（`Settings.tsx`）。数字与小图表（数字块、额度条、按天的柱、出刊日历、横条、走势线）在 `components/Figures.tsx`，三页共用；agy 登录面板 `components/AgyLogin.tsx` 挂在 App 上，告警里的「登录 agy」打开它；模型列表和机房的只读分工格在 `components/Routes.tsx`，设置页的行和表单在 `components/Knobs.tsx`，灯和读数（机房的系统列表与设置页同一套词）在 `box.ts`。全局样式与色板在 `styles.css`，频道页样式在 `channel.css`。

页面读的接口：导航角标与频道页的告警灯读轻量的 `/api/status`；刊读 `/api/overview`（全部期次、同时在印的每一期 `running`、`drain_seconds`；期次和看板一样由 `_issues` 一次批量读出，不按期逐条查，2000 期约 15 条查询）；机房读 `/api/engine`（10 秒）与 `/api/engine/stats`（60 秒）；设置页读 `/api/settings`（10 秒）；频道总览读 `/api/channels`（每个频道带 `stats`），看板读 `/api/channels/{cid}/board`；专题总览读 `/api/topics`（8 秒），一个专题读 `/api/topics/{id}`（有在读的 4 秒，否则 20 秒），一期读 `/api/topics/{id}/videos/{vid}`（在读时 5 秒）。轮询的页面在切回、重新获得焦点或恢复联网时立即再读一次（`hooks.ts` 的 `useBack`）；调试台空闲时每 60 秒、有试跑在跑或排队时每 15 秒读一次。

## 访问控制

web 只接受三种请求（`app.py` 的 `guard`）：Host 头是 localhost / 127.0.0.1 / [::1]（只看 Host 头，不看来源地址，懒猫上靠容器网络隔离）；懒猫网关转发且带登录用户头（`ASSETIZER_TRUST_LAZYCAT=1`）；配置了公共域名时带有效 Cloudflare Access JWT（OVH 时期留下的，懒猫上没有配）。POST 只收 JSON。

## 部署形态

当前生产在懒猫微服 prodbox（LPK：一个运行时镜像，web / worker 两个服务），开发机是 devbox；做法见 [operations.md](operations.md)。仓库里的 `deploy/`（OVH 的 systemd 单元与 `deploy.sh`）是迁移到懒猫之前的部署方式。
