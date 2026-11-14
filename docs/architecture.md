# 当前架构

课代表把长期关注的投资 / 金融长视频，按频道的学习目标拆成知识单元，生成文章、截图、视频片段、字幕和 Notion 页面。本页描述**现在的**系统结构；长期决定与理由见 [design.md](design.md)，频道页见 [channel-page.md](channel-page.md)。

## 进程与数据

web 与 worker 共用数据目录（`ASSETIZER_HOME`），hook-tunnel 只挂载自己的凭据目录：

| 进程 | 入口 | 职责 |
|---|---|---|
| web | `python -m assetizer.cli web` → `src/assetizer/web/app.py`（FastAPI） | 界面与 API；不跑生产流水线，但学习目标的编译（后台线程）和起草在这里调用模型，试跑入队时也直接派发第一条；机房的 agy 网页登录（`providers/agy_login.py`，伪终端里的 agy 登录界面）、手动读额度、频道的立即扫描（后台线程）、新专题读链接里的视频列表（yt-dlp，不下载）也在这里；专题的接口不调模型，只排进 worker |
| hook-tunnel | `scripts/lzc/hook-tunnel.sh` → 固定版本 cloudflared | 可选的 Notion 公网推送入口；没放 token 就等待，不连接 Cloudflare；与 web、worker 共用运行时镜像，独立服务只绑定 `/lzcapp/var/assetizer/hook-tunnel:/ingress`，读取 `/ingress/tunnel-token`；不配代理，也不以隧道连通性做应用健康检查 |
| worker | `python -m assetizer.cli worker` → `pipeline/runner.py` 的 `Worker` | 生产、交付、试跑与补录派发、自动收录、健康探测、读 agy 额度（每 15 分钟）、保留期清理（原始媒体；日志每天一次，见 [operations.md](operations.md)「日志与迭代」）、调试样片与专题视频的低清版（`playback.py`）、专题的读与综合（`topics/run.py`）、网盘备份与取回（`topic-backup` 线程，`topics/backup*.py` 调 rclone）、通知 |

数据目录里：`assetizer.db`（SQLite，WAL）、`runs/<run_id>/`（每次运行的产物与检查点）、`media/`（原始视频与工作文件）、`playback/`（调试样片和专题视频的 360p 低清版，供频道页原片栏播放，滚动释放）、`packages/`（发布素材包，每期一份，按最后一次跑到 PACKAGE 的运行重建）、`compile/`（编译时的模型答复）、`logs/`（yt-dlp 的原话，留 30 天）、`cache/magpie-media/`（magpie 的抽帧缓存，目前不清理）、`cache/gdrive-locks/`（网盘备份的规则锁与租约）、`issue-asks/<批次>/`（频道页批注的模型输入输出与断点）、`health/`（模型自检的临时目录）、`bench/`（基准的报告与媒体）、`topics/`（专题：`media/<视频>/` 原片与 `drafts/<视频>/` 底稿按视频存，`<专题>/r<修订>/<视频>/` 是每次读的计划、回看与笔记，`<专题>/synth/` 是综合的模型答复）、`secrets/`（凭据文件，见下）、`run/`（worker 心跳与正常退出标记）。

凭据只从环境变量 `ASSETIZER_<NAME>` 或 `secrets/<name>` 读取（`config.secret`）：`notion-token`、CookieCloud、Telegram、`magpie-api-key`、`tuzi-api-key` 等；网盘的 OAuth 客户端与登录只在 `secrets/rclone.conf`，只有 `topics/backup.py` 读写。仓库里不放任何凭据。

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
- CookieCloud 带着 YouTube 登录仍报私享或会员限定，或视频已删除，判「视频不可用」（`VIDEO_PRIVATE`、`VIDEO_MEMBERS`、`VIDEO_REMOVED`；地区限制之类是 `SOURCE_UNAVAILABLE`），刊上写明原因，可以「忽略」（运行转为 `CANCELLED`）。会员等级不够时 YouTube 只说 "This video is not available."（页面上是会员专享的提示），已登录时也算 `VIDEO_MEMBERS`，刊上写「会员权限不够」；没登录时等 YouTube 登录。其他原因失败的期同样可以「忽略」。视频下载好以后，后面的工序只用本地文件，中途下架也照常做完。
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
- 补录（`ingest.py` 的 `enqueue_backfill` / `dispatch_backfill`）：已定稿频道在看板里按日期挑旧视频（`/backfill/candidates?since=&until=`，web 在后台用 yt-dlp 列到够深，最多 1000 期，留 10 分钟），或用「贴链接」补一条列表里看不到的不公开视频（`/backfill/link`，先读视频确认是本频道的），进 `backfill_queue`；worker 只在有空闲路数、没有别的排队、没有试跑等着、且额度够时派（优先级 -1，`runs.origin = 'backfill'`）。`backfill_pace()` 算现在几路：平时一路；`backfill_hours`（东八区整点，设置页里涂）涂到的钟点和 `backfill_burst`（`{"until"}`，`null` 是不限时）生效期间 `backfill_fast_slots` 路（默认 8，不超过 `worker_slots`），到点后正在跑的跑完，降到一路再派新的，规则见 [design.md](design.md) §R6-5。`backfill_view()` 给机房和看板同一份：排队多少、在跑几期、几路、到几点、什么挡着（`backfill_stop()`）。回到调试期时取消排队的补录；「取消排队」和回到调试期只取消还没派出的期，worker 先把要派的那一期标成已派出、再去查库，正在派的那一期照常付印。看板的「试一期」用 `/candidates?limit=5`（接口上限 15，留 5 分钟）找最新还没收、不太短的一期，按正常优先级立即付印。

## 专题

专题（[topics.md](topics.md)、[design §R8](design.md)）不走上面的流水线、不建运行：每期在 `topic_videos` 里有自己的状态与步骤（`ACQUIRE → DRAFT → PLAN → LOOK → SECOND → WRITE`，有没把握的再 `LOOK2 → SECOND2 → REVISE`），读完再综合。worker 在派完生产和试跑、补录之前调 `topics.run.dispatch`：先派要做的综合，再在 `topic_slots` 之内派下一期；一期占一个并行槽（`Worker.active` 里的 `topic:<专题>:<视频>`），不占运行。分工用到的订阅低于专题的额度留底（`run.held`）时不派新的；槽满又有运行排队时，在读的一期在下一步之前让出槽（`run.crowded`）；同一个视频的底稿同时只有一个专题在录（`run.drafting`）。四道工序的模型由 `knobs.topic_routes` 决定，`providers.for_stage` 遇到 `draft`、`look`、`second`、`topic` 开头的工序名就走它，不看 `stage_providers`；频道调试期的底稿（`channel_draft` 开头，design §R-21）同样单独走 `knobs.channel_draft_route`，不算进生产的额度判断（`providers.routes`）。

专题的回答另存到 Notion 由 `topics/notion.py` 做：worker 在整批的综合做好后接着写（`run.put_together`），页面要求的写由 `dispatch` 起一个不占槽的短线程（`topic-notion:<专题>`）；复用 `notion/client.py`（插在某块之后、页内数据库、列出行）、`notion/blocks.py` 和 `delivery` 里认课代表身份、建行、记属性的函数，不经交付的计划与检查点。页上的块由共用的 `notion/sync.py` 的 `Sync` 逐块比对着写：只换课代表写过、内容变了、没人动过的块，别人写的和被人改过、评论过的都不动；「来源」表是页内数据库（`topics.notion_db`），每行的页面和写过的内容记在 `topics.notion_rows`。页上和各行里的评论由 `topics/comments.py` 读回：`POST /api/notion/webhook` 流式收最多 64 KB 原始字节，用 `notion/hook.py` 校验 HMAC-SHA256（常量时间比较），只将三种评论事件的路由元数据按 id 去重存进 `notion_events`，不在请求内访问 Notion。worker 的 `housekeeping` 每个 tick 检查待处理事件，启动独立的 `topic-hook` 后台线程，循环取事件至空，按批合并同块或同页讨论的定点读；原来的 `topic-comments` 线程仍每 10 分钟执行 `comments.due()`，检查各页是否到 6 小时，不受事件批次推迟。`put_together` 综合前仍整页读，定点读不改 `comments_at`；按专题的可重入锁保证同一专题不同时对账，不同专题互不阻塞；事件线跳过忙专题再检查，避免排在前面的忙专题挡住其他页。定点读只关闭同一页同一块消失的评论，保留旧 `under`，新行待整页读补小节。来源表的行按 `notion_rows` 归类；自己的 bot 跳过评论、块和页面读取，频道页批注由 `issue_asks.py` 按 §R3-12 定点读取，未知页、归档专题和范围外频道页不读。每批仅在有未归档专题、行页或已定稿且非已登记的频道页事件时查询一次 `/users/me`（只尝试一次、5 秒超时），不跨批缓存身份；任何查询异常都按身份未知继续定点读，其他客户端调用仍默认最多尝试 6 次。全是不在范围内的频道页、未知页或归档专题时连身份也不查。`notion_events.bots` 原样保留事件的 `authors` 数组，供事件的作者判断使用。频道定点读的 429、5xx、网络或超时失败在 notion_events 的 tries/retry_at 持久化退避，依次等 1 分钟、5 分钟、30 分钟、2 小时、6 小时；第五次重试仍失败才 warn 与终态，同次处理同源终态按标题与事件数合记一条 warn。专题处理出错记 warn 和终态，不重试，靠整页兜底；专题读到的评论记在 `topic_comments`；点名课代表的讨论串交给下一次综合（停笔 10 分钟后自动排一次），回复在写完 Notion 后发，每一句点名记在 `asks`（用量和待修清单）。

## 交付到 Notion

- 每个频道有自己的目标库与属性映射（设置 `notion:<频道>`，`notion/mapping.py`）；没有频道目标的期写入课代表自己的数据库（设置 `notion_database_id`，没有时在 `notion_parent_page_id` 下自动创建）。
- worker 在一期生产完成后，或空闲时，逐期交付（`delivery.deliver_pending` → `deliver`）。**调试中的频道不交付**，样片等定稿时按定稿单写入。
- 交付前核对映射（属性类型、选项是否存在）：不一致时该期 `BLOCKED`，原因以 `NOTION_MAPPING` 开头，频道显示「交付暂停」，修好映射后自动继续。
- 新频道的页面图标默认是频道头像（`icon: "channel"`，`channel_icons.py`），卡片上可以改：用 yt-dlp 取频道最大的方形头像地址，存在设置 `channel_avatar:<频道>`，worker 在后台线程里每周刷新（`channel_icons.refresh`），`sync` 和预览只读设置，不问 YouTube。频道总览、页头和图标卡片也显示这张头像。新页面建立时带上；旧页面由 worker 每分钟改最多 20 个，只改图标，`sources.notion_icon` 记着每页用的地址，地址变了会重写。
- 同一版内容调整、重印、换成新运行都用共用 `notion/sync.py` 的 `Sync` 在原页逐块同步，保留页面 id 和 URL。没变的块保留原 id；只替换课代表写过又没人改过的块，别人写的、改过的、里面加过子块的、带未解决评论的块原样保留，计划键相同的新内容紧跟对应旧块，键已变化时按内容和相邻块定位。频道页按源保存认过的课代表身份（`notion_authors:<视频>`），同页同步把已存集合、当前 integration 与映射中现存顶层块的作者取并集，在写块前落库；不吸收未映射块的作者，换新页时重置为当前身份。子块也用这个身份集合判断。文本按文字、链接位置、非默认注解规范化，相同格式的相邻段合并。折叠内递归判断，子块的编辑或评论也保护整个折叠；用户解决评论后，下次交付才清掉旧块。只在要替换或移除块时逐块查评论，403 当作没有评论；频道批注的收取与回复见 §R3-12。
- 媒体签名由类型、说明文字、文件内容 SHA-256 组成，不含本地路径或 Notion 文件 URL。已有媒体仅在作者和编辑者仍是课代表时用 `notion_blocks` 的签名；缺失或旧式签名不匹配，未受保护的媒体首次同步时替换一次。只给新块上传文件，每批最多 40 块、视频结束一批，图片可与文字同批；该批上传紧挨 append。
- append 返回可能含后续兄弟块，只取最前面的本批新块并核对类型和新 id，再立刻把这一批的计划键、块 id、hash、实际计划的 run_id 与 template 用同一语句原子写入 `notion_blocks`，保留的配对块也更新全部映射字段；一个块只保留一行映射：写每一行前在同一事务里删掉同一来源、同一块 id、键不同的旧行（只按块 id 删，不碰别的块接过去的同键行），遗留的多行按没有计划键处理；删除的块和不再对应计划键的记录清掉。重试从实际页面匹配已写入的块；读回校验未受保护的课代表块按顺序等于计划形状、媒体为上传文件，成功后更新 `notion_synced_run`。按段写完就删除该段的旧块；一次同步内缓存子块，写删后使父块缓存失效，媒体校验合并到读回。页首插入使用 `position: start`，未变首块不重写。属性只改仍是上次写入值的（设置 `notion_props:<视频>`），基准只更新本次实际写过的属性，你改过的属性持续保留。PATCH 前按属性 id 记录待确认的规范值（`notion_props_pending:<视频>`），成功推进基准后清掉；回答丢失后，当前值与待确认值相同的按上次写入已生效处理，先恢复基准，再写下一版。仅规范化 title、rich_text 的文字、select 的 name、date 的 start/end（带时间的按同一时刻比较：Notion 把 `10:30+00:00` 读回成 `10:30:00.000+00:00`；只有日期的仍是日期）、url、number，其他类型保守视为不匹配；换新页时清掉待确认记录。
- 按视频查重依赖目标库里映射为「视频链接」的属性。批量查询（`registered_many`、`hand_written`）翻页读完；交付时查单个视频（`registered`）只读第一页（10 条）。查到的页是课代表自己建的、又没被别的期认领（建页的回答丢了）时，认领下来接着续传。
- 同步前先读页面，已进回收站或返回 404 时清掉页映射并另建一页，记录事件；在查重前处理。换库仍在新库新建一页，旧库的页原样保留。旧换页确认状态迁移为 `STALE`，下次交付就地同步。
- 几种情况先不写、停下等你（`HELD`，原因在 `delivery_reason`，刊的「需要你」里各有一行）：
  - `PARTIAL`：当前版本是「部分完成」（`COMPLETE_PARTIAL`：一道非阻塞工序整体记了 `WARN`）。不管从定稿、钉住还是重新交付过来都不写；重试这一版跑完整后照常交付（`adopt_completed` 解除）。工序内部的局部失败不算：部分片段剪辑失败、截图失败、预审某批失败时仍是 `COMPLETE`，照常交付，片段和截图的失败记成校注。
  - `MAYBE_HAVE`：补录进来的期，库里有这个频道没填视频链接的页（手写的）可能就是它。按日期起标题的频道（汤姆）比「内容所属 + 日期」，日期取 YouTube 的上传日、纽约当天和标题里的 MM/DD；用视频原标题的频道（王海滨）比标题。你选「照写」（另写一页）或「算已有」（登记为 `REGISTERED`）。补录列表里同样的比对标「可能已有」，默认不勾（列表的日期是近似值，只用标题里的 MM/DD）。
- 映射按属性 id 记；在 Notion 删掉一个属性又建了同名同类型的，库里只有这一个、也没被别的映射用上时自动接上（`mapping.sync`），类型变了或已被占用仍要重新选。
- 频道的 Notion 设置带修订号（`rev`，保存和换库时加一，跟随 Notion 里的改名不加）。页面保存时带回读到的修订号，对不上返回 412，界面让你选「用我的覆盖」或「放弃我的」。
- 已写进 Notion 的视频再贴一次，不重印：返回那一页（`kept`），要重印时带 `reprint` 再提交。

频道页批注不走专题综合，也没有整页扫描。housekeeping 每个 tick 便宜地查到停笔的请求，起一条 `issue-asks` 后台线程，逐页处理；定点读取、冻结批次、应用结果和回复共用按 source 的可重入锁，调用模型时不持锁，同页新事件可在模型返回前完成定点读；覆盖索引与按讨论串分组查询让每 tick 不需逐行相关查询。`issue_comments` 对账只关闭当前页当前块的评论；`issue_batches` 冻结模型输入和已接受输出（LLM 的原子 accepted 缓存也能恢复），应用结果时一次事务写 `issue_fixes`、`asks` 和 `issue_replies`。模型只调「批注」分工，不走视频工序或生产额度与登录判断；与生产和专题共用回执记账器，保留供应商身份和推理用量，记在 issue-<source_id> 下，不混入采用运行的 Token。错误没有 provider 自动重试，首调加一次重生成或格式修复，最多两次请求；失败后等新话。APPLY 的临时 SQLite 错误回滚后保留已收输出，下个 tick 重做应用事务，同批只 warn 一次。DONE 删除调用目录，FAILED 留着排查。输入最多 32 串、每串 128 句、每句 2000 字、块 1800 字、每节文章 12000 字与校注 4000 字、整页转写 40000 字、内部冻结输入合计 120000 字符；超限串数或总量使整页失败，不拆成多次调用；截断字段明确标记。节上下文按节 id 只放一份，讨论串只引用节 id。无法定位到节的页讨论、用户块或节外块只回模型的话，不补造原话、不附链接；有原话的已答、没改必须指定 line，否则合同校验不通过，时间由代码取那行起点。

预览和交付统一由 `delivery.page_plan` 在模板与页尾之后套 `issue_fixes`，`build_plan` 再算 hash。改正保存 source、run、模板名、计划键与子块路径提示、未套改正的原文和 SHA-256、节 id、新文字；同一目标后写覆盖先写，原文始终取模板原稿。改正只在记下时的同一运行、同一模板有效，同版重印保留；换模板和换运行一样不带过去，成功交付读回时作废并在原串说明。代价是原文没变的段落，换模板后改正也带不过去。讨论串运行不变、模板变化时，先按记下的旧模板构建未套改正的计划并核对旧位置，只在旧版、新版本节里该原文各唯一一处时重新定位；同一批两串及以上汇合到一块，只要有换模板迁移来的串，全部不能已改。同运行、同模板的串仍只核对原位置；找不到时 plan_text 留空、不记原稿、不更新位置。模型冻结与应用时的运行或模板不同，已改转成固定作废说明，不写改正、不更新位置。改正应用与旧改正替换只调严格的 `_find`；只有讨论串定位提供旧模板计划，且同一批同一旧模板只构建一次。只换 rich_text、沿用当前块类型，不改 `sections` 或 `composition.json`。`sources.notion_synced_run` 和 `notion_synced_template` 在交付完成的同一事务写下本次计划版本，换页重置时同时清空；扫描从提供计划键的同一条 `notion_blocks` 记录取运行与模板（向上找父块时取父块那行；被批注的块或途经的块有多于一行映射时，按没有计划键处理且不再往上找），因此未完成或失败的交付也不混用上次成功版本；仅旧映射两列都为空时，才退回 sources 的这两项，模板仍为空再退回当前设置。`issue_positions` 按来源与讨论串保存扫描时块映射的运行、模板、节及位置，首次定位后保存原稿；追问用原稿定位，重新找到后在应用事务更新位置；定位失败不提供错位的 plan_text、不能已改。替换只合并同一目标位置上的旧改正；表不对提示键作唯一约束。预览和建计划不写告警，只有成功读回作废时按标题和条数合记一条。折叠子块在两层父链内找到计划键后，用块类型和原字在本地计划相应深度唯一定位，只改该子块，保留父块与兄弟块；用户子块、找不到或有歧义的子块不能已改，同批不同子块的改正合在同一父计划项中。每条改正有递增 id，计划携带建计划时所有已检查 id 及实际套用 id；Sync 读回与属性写完后，完成事务只放行实际套用的改正回复，未套上的已检查改正在唯一 `_void` 入口删除并作废。未发的改写为固定的「没改」通知，已发的排一次新通知，`void_of + discussion` 唯一约束避免重复；切换运行或模板后带不过去的改正不复活。新写改正不在旧计划检查集合中，完成后仍保持 `STALE` 再交付。`issue_replies` 是持久化 outbox，立即结果先可发，已改等读回；发送前在同一来源锁内再次调用 `_void`，检查待回复所属运行、改正是否存在及读回标记。`issue_replies.version` 在作废、内容或改正依赖改写时递增并清 sending；POST 前按版本与未完成条件认领、记 sending，完成按同一版本标 done；若版本变了只保存发出的评论为 ours，新版本仍待发送。结果未知先等满 10 分钟，再定点读同串、用固定回复内容及已记账 id 对账；这依赖 Notion 届时能列出已成功的评论。作废通知与未知结果恢复遇到已解决或删除的串不再补发；403 留等，下一轮回复检查至少隔 10 分钟，400/404 丢掉该条。换页的旧串不移到新页。偏好与重做不实现。

## 主要数据表

`channels`、`channel_prompts`（学习目标版本）、`playbooks`（规程版本）、`sources`（每一期：采用的运行 `current_run_id`、是否钉住 `adopt_pinned`、交付状态 `delivery`、题眼 `headline`）、`runs`（`origin`：manual / ingest / backfill / trial / lock（定稿单排入或等着的），2026-10-28 之前的为空；`stop_at`：「清空」要它停下）、`stages`、`backfill_queue`（补录）、`units`（知识单元）、`sections`、`assets`（截图、片段、海报）、`flags`（校注）、`samples`（调试样片）、`trial_queue`、`notion_blocks`、`alerts`（`notified_at`：推送过没有，非紧急的等 10 分钟）、`notify_outbox`（没发出去的 Telegram 通知）、`decisions`、`events`、`settings`、`model_calls`、`speakers`、`run_votes`（旧的「喜欢 / 备注」，只保留数据）；专题的 `topics`（问题、修订号、综合、你的标记、盯着什么、归档、选片篮的批次、存到 Notion 的目标与那一页）、`videos`（专题的视频，原片与底稿按视频，几个专题共用；`gone` 是 YouTube 上没了）、`topic_videos`（一个专题里的一期：状态、步骤、判断、是否算进回答和为什么）、`topic_comments`（批注、点名、处理版次）、`asks`（专题与频道共用的用量和待修，`source_id` 标明频道期次）、`issue_comments`、`issue_positions`、`issue_state`（失败时间与权限状态）、`issue_batches`、`issue_fixes`、`issue_replies`、`notion_events`（webhook 路由元数据、处理状态，日志 retention 固定清理 30 天前的行；不存请求体、签名、令牌）、`topic_versions`（每次综合的一版）、`topic_removed`（移出后暂存 7 天的期）、`topic_candidates`（盯着的频道和关键词里冒出来的新视频）；`retries`（模型调用的自动重试与救回，§R6-10）；网盘备份的 `video_backups`（按视频的副本状态，频道与专题共用）、`backup_authors`（按作者的传 / 不传规则；专题的规则在 `topics.backup`）。表结构与迁移都在 `db.py`（启动时自动补列）。

## 前端

React 19 + Vite + TypeScript，源码在 `web/src`，构建产物写入 `src/assetizer/web/static`（不进 Git）。页面：刊（`Issues.tsx`）、对照视图（`Reader.tsx`）、频道（`Channel.tsx`：总览 `components/channel/Overview.tsx`、看板 `Board.tsx`、调试台与其余 `components/channel/`）、专题（`Topics.tsx` 总览、`Topic.tsx` 一个专题、`TopicVideo.tsx` 一期，组件在 `components/topic/`，样式在 `topic.css`）、机房（`Engine.tsx`）、设置（`Settings.tsx`）。数字与小图表（数字块、额度条、按天的柱、出刊日历、横条、走势线）在 `components/Figures.tsx`，三页共用；agy 登录面板 `components/AgyLogin.tsx` 挂在 App 上，告警里的「登录 agy」打开它；模型列表和机房的只读分工格在 `components/Routes.tsx`，设置页的行和表单在 `components/Knobs.tsx`，灯和读数（机房的系统列表与设置页同一套词）在 `box.ts`。全局样式与色板在 `styles.css`，频道页样式在 `channel.css`。

页面读的接口：导航角标与频道页的告警灯读轻量的 `/api/status`；刊读 `/api/overview`（全部期次、同时在印的每一期 `running`、`drain_seconds`；期次和看板一样由 `_issues` 一次批量读出，不按期逐条查，2000 期约 15 条查询）；机房读 `/api/engine`（10 秒）与 `/api/engine/stats`（60 秒）；设置页读 `/api/settings`（10 秒）；频道总览读 `/api/channels`（每个频道带 `stats`），看板读 `/api/channels/{cid}/board`；专题总览读 `/api/topics`（8 秒），一个专题读 `/api/topics/{id}`（有在读的 4 秒，否则 20 秒），一期读 `/api/topics/{id}/videos/{vid}`（在读时 5 秒）。轮询的页面在切回、重新获得焦点或恢复联网时立即再读一次（`hooks.ts` 的 `useBack`）；调试台空闲时每 60 秒、有试跑在跑或排队时每 15 秒读一次。

## 访问控制

web 只接受三种请求（`app.py` 的 `guard`）：Host 头是 localhost / 127.0.0.1 / [::1]（只看 Host 头，不看来源地址，懒猫上靠容器网络隔离）；懒猫网关转发且带登录用户头（`ASSETIZER_TRUST_LAZYCAT=1`）；配置了公共域名时带有效 Cloudflare Access JWT（OVH 时期留下的，懒猫上没有配）。POST 和 PUT 只收 JSON。另有 Host 为 `notion-hook` 的独立分支：仅允许原始路径精确等于 `/api/notion/webhook` 的 POST，方法、尾斜杠、点段、重复斜杠和编码变体一律 404；不需要登录头或 Access JWT。LazyCat 和 PUBLIC_HOST 都不能访问 webhook；推送状态 `/api/notion/hook`、复制验证令牌 `/api/notion/hook/token` 和重新接收 `/api/notion/hook/rearm`、放隧道 token `/api/notion/hook/tunnel`（这几个在 `web/notion_hook.py`）只给本机或已登录 LazyCat 网关，隧道 Host 下全部 404。接收端全进程共享 120 次/分钟的滑动窗口，超额 429 带 Retry-After。验证令牌在 `secrets/notion-hook-verification` 用 0600 临时文件原子替换，首次自动收，相同令牌静默确认，不同的已有令牌须在 15 分钟窗口内换；接收锁、专用数据库连接的写锁各有 5 秒等待预算，超时 503，其他数据库连接不受影响。settings 只存时间和事件计数（含 rejected_at），换令牌清空拒收计数和拒收时间；换后第一次有效签名的时间说明订阅已开始工作。

## 部署形态

当前生产在懒猫微服 prodbox（LPK：一个运行时镜像，web / worker / hook-tunnel 三个服务），开发机是 devbox；做法见 [operations.md](operations.md)。
