# Assetizer · IA + Technical Design v0.2

> 产品输入：PRD v0.2 · Post-Validation Baseline（Notion，生效）+ Product Validation Closure + Run-001～014 本地证据。
> 本文件合并 IA 与 Technical Design 的第一版，供实现直接使用；ADR 级裁决在 §12 列出。


> **v0.2 = v0.1 草案 + 独立评审（docs/design-review.md）采纳结果。** 下面"§R 评审后裁决"优先于正文中冲突的描述。

## §R 评审后裁决（2026-10-05）

**采纳：**
1. 失败矩阵：空响应 / 仅 `}` → 同 prompt 重生成 1 次（不走 repair）；`AGY_AUTH_REQUIRED` / `PROVIDER_QUOTA` → **暂停队列**，Run 进 `WAITING`，不计失败；Worker 恢复前杀掉孤儿 agy 进程组（DB 记录 pgid）；媒体调用超时 35 min + 5 min 无流事件看门狗。
2. **时间合同**：模型只输出 Chunk / Window 内的局部时间（它在画面里看到的时间码）；全局时间由代码 = local + 实测 offset（ffprobe 首帧 pts）；所有时间夹在 `[0, duration]`；记录 precision。Merge / Knowledge 的时间由代码从 topic 引用推出，不让模型做加法。
3. **KNOWLEDGE 与 ARTICLE 分片**：KNOWLEDGE = skeleton（只出 unit 列表）+ detail（每批 6 个）；ARTICLE = outline + section 批次，产物为 `ArticleSection[]`（自带 unit_refs）。
4. **IA**：新增 `Flag`（内容待判断项）、`Alert`（系统健康，可自动解除）、`Decision`（需用户拍板）；状态拆为 `production` / `delivery` / `review` 三字段；Notion 行与 Package 属于 **Source**（指向 current_run），重跑不产生第二篇；KU 主键为系统 ULID，模型 slug 只作 label；`publication_priority` / `selection` / `embed` / `standalone` 挂在 Clip Asset；KU 增加 `depends_on`、`segments`、`facets`（频道特有字段由 Playbook 声明）；新增 Speaker Registry。
5. Regenerate 两个动词：「只改这一处」（本 Source 新 Revision，不改 Playbook）与「以后这个频道都这样」（PromptAmendment 提案 → 新 Prompt 版本）；Minor Edit = 确定性操作（ASR 词边界微调 Clip、文本 overlay）。
6. Stage 顺序 RESOLVE → PLAYBOOK → ACQUIRE；格式 `-S res:1080,vcodec:h264,acodec:aac`，高度 < 720 → `DEGRADED_FORMAT`；Chunk 保持 50+5（agy RSS 与媒体时长正相关，50 min ≈ 7.6 GiB）。
7. Boundary：窗口重编码 720p 保证 0 点；模型返回窗口内时间；**全片 ASR 作为 Source 级 Transcript**，用 Run-008 句界强度做有方向吸附（起点 [-6,+2] s 优先向前，终点 [-1,+6] s 优先向后），不越过下一 KU 起点；容差内无合格句界 → `BOUNDARY_UNCERTAIN` Flag；模型失败 → 确定性兜底 + `BOUNDARY_FALLBACK` Flag（总要出片）。
8. 新增 **REVIEW_CHECK**：一次批量纯文本调用，检查 Clip 首尾半句、代词悬空、结论前结束、下一主题混入，以及 KU 的 PLAN/EXECUTED/条件 claim 与 ASR 对照（CAVA 类）→ 只产出 Flag，不改写。
9. Clips：`-r 30 -pix_fmt yuv420p -profile:v high -g 60 +faststart`；删除 silencedetect，改 ASR 首末词裁剪；每段生成 poster。截图：锚点 ±1 s 取 5 帧选最清晰。MEDIA_QA 加查 codec/profile/pix_fmt/moov 位置。
10. COMPOSE 按 Source 时间顺序；全部 GENERATED Clip 内嵌；小节内顺序 视频 → 截图 → 文字。
11. Notion：数据库行（每 Source 一行，状态用属性）+ block_key→block_id 映射 + 续传前读回对账 + JIT 上传；交付独立于生产，token 未配置时 `delivery=BLOCKED`，应用内照常审阅。
12. 磁盘：剩余 ≥ max(15 GB, 6×源文件) 才允许 ACQUIRE；subtitled 版本可重渲染，审阅完成 N 天后删除。
13. Playbook：今晚导入 Run-014 已审计的 Tom Playbook 为 ACTIVE；王海滨导入 Run-013 W 为 NEEDS_CONFIRMATION；编译器采用"确定性切条款 + 逐条归属 + 代码算覆盖率 + 规范化 quote + Policy 冲突检测 + 用户确认一次"，后续实现。Universal = 空 ChannelPrompt 的 Playbook（Run-011 Universal 指令）。
14. 导航收为 3 个：**刊**（期刊目录 + 粘贴框 + 印刷线）/ **频道** / **机房**。视觉概念「刊 + 朱批」：纸墨双色 + 唯一强调色朱红；中文衬线 Noto Serif SC；Reader 三栏（纵向 Source Timeline 脊柱 / 文章 + 单一悬浮播放器 / 朱批边注）。
15. agy 调用记录 agy 版本；Web 只走 SSH 隧道 → 今晚不做移动端 QA。

16. **模型输入一律为 Proxy**（2026-10-05 实测后裁决）：agy 自动升级到 1.2.16 后 `view_file` 拒绝 >100 MB 文件，Run-014 式 619 MB 流复制 Chunk 不再可读。CHUNK 改为从原片精确起点重编码 1 fps、原分辨率、GOP 60 s、CRF 32 的 Proxy（50 min ≈ 92 MB，Ticker/价格清晰可读），超限按 CRF 36 → 720p 阶梯降级；Boundary 窗口 2 fps。副作用：offset 精确等于起点（不再依赖关键帧），agy 峰值内存显著下降。
17. **`--json-schema` 只用于小输出**（同日实测）：整段理解在 schema 模式下把输出预算耗在 thinking、返回空 topics；contract repair 随后编造了占位主题。裁决：大输出阶段把 Schema 写进 Prompt 并本地校验（Run-014 路径）；内容为空 / 不合规 / 工具偏航 → 重新生成一次；repair 仅限无法解析的文本，且不得编造。

**推迟：** Playbook 编译器与 Onboarding UI、轻量 Discovery（transcript-based）、RSS 自动收录、PromptAmendment、M lane 并行、媒体 proxy A/B、systemd-run scope（先确认 user systemd 可用）。

## 0. 设计约束（来自用户与证据）

- 单台 OVH VPS：6 vCPU / 12 GB RAM / 无 swap / 99 GB 盘。**串行处理**，同一时间只允许 1 个 Source 在流水线中（Run-013 并发实验：H=1 默认）。
- 模型路径固定为 **agy CLI 登录态**（Antigravity，`gemini-3.8-flash-high`），通过 `ModelProvider` 接口抽象，未来可换 Gemini API Key。
- agy 每次带视频调用在 `~/.gemini/antigravity-cli/{conversations,brain}` 留下 ~600 MB；**每次调用结束、产物落盘后必须按 conversation_id 删除**。
- YouTube：服务器 Chromium 持久化 profile + 用户通过 noVNC 远程屏幕登录；yt-dlp `cookiesfrombrowser` 读取。认证失败必须分类，不能把所有 403 当成"请登录"。
- Notion：生产 token 用户之后配置；当前用临时 token 测试。
- Run-014 中由 Codex 人工完成的两步必须自动化：Playbook Fidelity Audit、Boundary Sanity Check。
- Run-014 的 clip/字幕在 Mac（mlx_whisper / videotoolbox）上跑；正式版全部在 Linux CPU 上：faster-whisper（int8）+ libx264。
- UI 品质标准：Awwwards / Webby / FWA 获奖水准，反复自审直至达到。

## 1. 信息架构（IA）

### 1.1 核心对象

```
Channel ──< ChannelPrompt(version) ──< Playbook(version, compiled, audited)
   │
   └──< Source ──< Run ──< Stage(checkpoint)   ──< ModelCall(telemetry)
                    │
                    ├──< KnowledgeUnit  (canonical semantic object)
                    │       └──< Asset  (screenshot | clip | subtitle)
                    ├── Article (markdown, KU markers)
                    ├── Delivery: NotionPage, PublicationPackage
                    └── Review (run-level decision + asset-level overrides)
Attention (系统级待办：YouTube 认证、agy 登录失效、磁盘、Playbook 待确认、Run 失败)
```

| 对象 | 说明 | 关键属性 |
|---|---|---|
| Channel | 一个 YouTube 频道或其他来源作者 | mode: `TRACKED` / `DISCOVERY`；youtube_channel_id；display_name；delivery target |
| ChannelPrompt | 用户 Raw Prompt，**用户意图 Source of Truth**，不可变版本 | text, sha256, origin(notion_url / manual), created_at |
| Playbook | Raw Prompt 编译出的 Runtime Playbook | rules[{id, rule, quote, phases}], requirements[], fidelity[], audit, status: `DRAFT/NEEDS_CONFIRMATION/ACTIVE/SUPERSEDED` |
| Source | 一个视频 | url, video_id, title, published_at, duration, media(path, sha256, bytes), media_state: `ABSENT/CACHED/PURGED` |
| Run | 用固定 Playbook 版本处理一个 Source 的一次执行 | kind: `PRODUCTION`/`DISCOVERY`；status；stage；playbook_id；telemetry |
| KnowledgeUnit | 产品核心对象 | 见 §5.2 Schema，`learning_priority` 与 `publication_priority` 分离 |
| Asset | 截图、Clip（clean / subtitled）、SRT/ASS | kind, status: `GENERATED/WARNING/FAILED/NOT_SELECTED`, path, sha256, lineage(unit_id, source range) |
| Review | 用户判断 | run decision: `PENDING/APPROVED/CHANGES_REQUESTED/REJECTED`；asset overrides；notes；active_seconds |

### 1.2 导航（Web UI）

1. **Desk（今日）**——默认首页。只回答"现在有什么需要我判断？"
   - Attention 条（系统问题，最高优先）。
   - Ready for Review（按完成时间）。
   - In production（当前唯一运行中的 Run：阶段轨道 + 预计剩余）。
   - Queue（排队中的 Source）。
   - 一个永远可见的输入框：粘贴 URL → 提交。
2. **Library（资产库）**——Channel → Source → Reader。
   - **Reader** 是 Knowledge Unit 的第一个 Renderer：正文 + 内嵌 Clip + 截图，旁边是贴边 **Source Timeline**（整段视频的时间轴，KU 区段、Clip 条、截图刻度），随阅读位置联动——可追溯性做成界面本身。
   - Review 操作在 Reader 内完成（Approve / Request changes / Reject；单个 Clip：Approve / Reject / 调整 Publication Priority）。
3. **Channels（频道）**——Tracked / Discovery 两栏。
   - Channel 详情：Raw Prompt 编辑器（版本列表、diff）、Playbook（规则、quote 溯源、Fidelity 矩阵）、确认按钮。
   - Onboarding：Discovery 报告 → "加入长期关注" → "你主要为什么看这个频道？" → 生成 Raw Prompt 初稿 → 编译 → 确认一次。
4. **Runs（运行）**——所有 Run 的生产记录：阶段时间线、模型调用、token、峰值内存、失败分类、重试按钮。
5. **System（系统）**——agy 状态、YouTube 会话（含 noVNC 登录入口说明）、磁盘与媒体保留策略、Notion 目标配置、版本。

### 1.3 主流程

```
粘贴 URL ─▶ resolve 元数据 ─▶ 匹配 Channel
   ├─ TRACKED 且 Playbook ACTIVE ─▶ 排队 PRODUCTION Run
   └─ 其他 ─▶ 排队 DISCOVERY Run ─▶ Discovery 报告
                    └─ 用户"加入长期关注" ─▶ Onboarding ─▶ Playbook ACTIVE ─▶ （可选）对该 Source 再跑 PRODUCTION
PRODUCTION Run ─▶ …自动… ─▶ Notion 页面（标注"待审"）+ Publication Package ─▶ READY_FOR_REVIEW
Review ─▶ APPROVED（Notion 状态更新、Source 媒体进入保留期后清理）
```

**交付策略**（ADR-4）：默认 `auto`——生产完成即自动写 Notion 并在页首标注"待审阅"，与 Run-014 验证路径一致，`human_manual_upload_count = 0`；可按 Channel 切换为 `after_approval`。

## 2. 系统架构

```
┌────────────── OVH VPS (debian, user=debian) ──────────────┐
│ systemd --user                                            │
│  assetizer-web.service   FastAPI + 静态前端  127.0.0.1:8770 │
│  assetizer-worker.service 串行 Job Runner（单进程）          │
│                                                           │
│ /home/debian/assetizer/                                   │
│   app/        代码 + venv                                  │
│   data/assetizer.db   SQLite (WAL)                        │
│   data/media/<video_id>/source.mkv     原始媒体缓存          │
│   data/runs/<run_id>/…                 每 Run 工作目录       │
│   data/packages/<run_id>/…             Publication Package │
│   data/secrets/ (600)  notion token 等                    │
└───────────────────────────────────────────────────────────┘
访问：ssh -L 8770:127.0.0.1:8770 ovh-investment → http://localhost:8770
（之后可选 Cloudflare Tunnel + Access，需用户确认）
```

- Web 与 Worker 分进程，只通过 SQLite 通信（jobs 表 + runs 表 + events 表）。Worker 崩溃不影响 UI；UI 重启不影响生产。
- Worker 主循环：取最早 `QUEUED` 的 Run → 依次执行 Stage → 每个 Stage 成功写 checkpoint。启动时把 `RUNNING` 但进程已不在的 Run 标为 `INTERRUPTED` 并从最后 checkpoint 自动续跑（一次），再失败就停在 `FAILED` 等用户。
- 前端：Vite + React + TypeScript，构建为静态文件随后端发布；无 UI 组件库，自有设计系统。
- 实时：前端轮询 `/api/events?after=` 长轮询（简单可靠，SSE 可后补）。

## 3. 目录结构（仓库）

```
assetizer/
  pyproject.toml
  src/assetizer/
    config.py          路径、模型名、阈值（env + data/config.toml）
    db.py              SQLite schema + 迁移 + 访问函数
    models.py          Pydantic：KnowledgeUnit 等 Machine Contract
    providers/agy.py   ModelProvider：agy 调用、schema 强制、遥测、会话清理
    contracts/         每个 Stage 的 JSON Schema + prompt 模板（中文）
    pipeline/
      runner.py        Worker 主循环、Stage 调度、checkpoint
      resolve.py acquire.py chunk.py understand.py merge.py knowledge.py
      article.py select.py screenshots.py boundary.py clips.py subtitles.py
      compose.py notion.py package.py cleanup.py discovery.py
    playbook/compiler.py   requirement 抽取 → 编译 → quote 校验 → 独立审计
    media/ffmpeg.py asr.py subtitle_algorithm.py
    notion/client.py blocks.py   API、multipart 上传、markdown→blocks、readback
    youtube.py         yt-dlp 封装 + 失败分类
    web/app.py         FastAPI 路由
    cli.py             `assetizer submit|status|worker|import-run|doctor`
  web/                 前端源码
  deploy/              systemd units、install.sh、sync.sh
  tests/
  docs/design.md
```

## 4. Pipeline（PRODUCTION Run）

每个 Stage：输入=上游冻结产物（sha256 记录）；输出=Run 目录下文件 + DB 记录；幂等（产物存在且 hash 匹配即跳过）。

| # | Stage | 实现要点 | 阻塞？ |
|---|---|---|---|
| 1 | RESOLVE | `yt-dlp -J`（cookies from profile），得到 channel_id、title、duration、upload_date | 是 |
| 2 | ACQUIRE | ≤1080p `bv*[height<=1080]+ba`，mkv；按 video_id 缓存 + sha256；失败分类（§7） | 是 |
| 3 | PLAYBOOK | 若 Channel 的 ACTIVE Playbook 存在则冻结引用；否则 Run 停在 `BLOCKED_PLAYBOOK` | 是 |
| 4 | CHUNK | 时长 ≤ 55 min 单 Chunk；否则 50 min + 5 min overlap，stream copy | 是 |
| 5 | UNDERSTAND | 每 Chunk 一次 agy 调用，`view_file` 仅一次、仅允许该文件；`--json-schema` | 是 |
| 6 | MERGE | 只读结构化记录；多 Chunk 时执行，单 Chunk 直接透传 | 是 |
| 7 | KNOWLEDGE | 生成 Knowledge Units（§5.2） | 是 |
| 8 | ARTICLE | Markdown，`<!-- KU:unit_id -->` 标记；禁用词检查 | 是 |
| 9 | SELECT | 每个 KU：SELECT / REJECT / CONTEXT_DEPENDENT | 否（失败则无 Clip） |
| 10 | SCREENSHOTS | ffmpeg 抽帧，每 KU ≤ 2 | 否 |
| 11 | BOUNDARY | 局部窗口（±30 s）agy 精修 → **自动 sanity**：窗口 ASR 词时间戳，把起点吸附到 ±6 s 内最近的句首停顿（gap ≥ 0.35 s），终点吸附到句末停顿；记录 delta；>10 s 标 major | 否 |
| 12 | CLIPS | 从 Original Source-global 时间直接切；libx264 CRF 22 / preset veryfast / AAC 128k / faststart；只裁剪真正首尾静音（silencedetect −35 dB, ≥ 3 s） | 否 |
| 13 | SUBTITLES | faster-whisper large-v3-turbo int8；Run-008 冻结断句算法；SRT + ASS + 烧录 720p 版；失败 = `FAILED_NON_BLOCKING` | 否 |
| 14 | MEDIA_QA | ffprobe + 全解码 + 首尾帧 | 否 |
| 15 | COMPOSE | 精确 KU 标记组合 Section ↔ KU ↔ Screenshot ↔ Clip，不做标题模糊匹配 | 是 |
| 16 | NOTION | 页面创建 + 文件上传（≤20 MB 单段，否则 multipart 10 MB 分片）+ 分批 append 带 checkpoint + readback 校验 | 是（交付） |
| 17 | PACKAGE | 目录结构 + manifest(sha256) + README | 是（交付） |
| 18 | CLEANUP | 删除 chunks、boundary windows、wav、agy 会话；Source 媒体按保留策略 | 否 |

DISCOVERY Run：1→2→4→5(Universal 上下文)→DISCOVERY_REPORT→18。不生成 Clip，不写 Notion（可选）。

### 4.1 模型调用合同（来自 Run-014 经验）

- 每次调用：独立目录 `calls/<stage>/attempt-N/`，保存 prompt、schema、stream-json 原始输出、response、invocation.json（session id、实际模型、tokens、wall time、峰值 RSS、view_file 次数、deviation）。
- 结构化阶段一律带 `--json-schema`；仍做本地 JSON Schema 校验（provider SUCCESS 但不完整 JSON 必须拦截——Run-013 S03）。
- 失败处理：最多 1 次同模型 **contract repair**（无媒体、只做格式转换，不补事实）；transient（429/5xx/超时）指数退避最多 2 次；`MODALITY_ROUTING_DEVIATION`（调用了非允许工具）立即终止该调用并记为失败；`PROVIDER_OUTPUT_LIMIT` 分类单列。
- Prompt 超过 120 KB 用 stream-json stdin 传输（Run-014 Linux argv 限制）。
- 内存：`GOMEMLIMIT=6GiB`、`GOGC=20`；Worker 串行保证同时只有一个 agy。
- 调用结束、产物落盘后删除 agy 会话目录（按 conversation_id）。

## 5. 数据合同

### 5.1 Playbook 编译（替代 Codex 人工审计）

1. **REQUIREMENTS**：从 Raw Prompt 抽取核心需求清单，每条带逐字 quote（代码校验 quote 是原文子串）。
2. **COMPILE**：生成 rules[{id, rule, quote, phases[]}] + orchestration_notes + article_style；quote 必须逐字存在于原文（代码校验，失败即 `PROVENANCE_FAILURE`）。
3. **AUDIT**：独立调用，只看 requirements 与 rules，逐条判 `PRESERVED/PARTIAL/DROPPED`。
4. 规则：`DROPPED = 0` 且 quote 全部校验通过 → `ACTIVE`（可按 Channel 设置为必须人工确认）；否则 `NEEDS_CONFIRMATION`，Desk 出现 Attention。

### 5.2 Knowledge Unit（canonical）

```json
{
  "unit_id": "unit_07_tsla_gap_up_breakout",
  "title": "…", "speaker": "…", "tickers": ["TSLA"],
  "source_start": 1234.5, "source_end": 1388.0,
  "knowledge_type": "TEACHING_EXAMPLE",
  "learning_priority": 3,                       // 1–3 星
  "publication_priority": "P1|P2|P3|ARCHIVE",
  "summary": "…", "teacher_reasoning": "…",
  "visual_evidence": ["…"],
  "higher_timeframe_setup": null, "lower_timeframe_trigger": null,
  "screenshot_anchors": [{"t": 1250.0, "reason": "…", "timeframe": "D", "visible_structure": "…"}],
  "observation_tasks": ["…"],
  "clip_candidate": true, "required_context": "…",
  "claims": [{"kind": "STATEMENT|VISUAL_FACT|INTERPRETATION|PLAN|EXECUTED|HYPOTHETICAL", "text": "…", "t": 1260.0}],
  "uncertainties": ["…"]
}
```

时间一律用秒（float），渲染时再格式化；Run-014 的 `HH:MM:SS` 字符串是解析错误来源之一。

### 5.3 Notion 页面结构

页首 callout（Source、时长、Run 状态"待审阅"、可信度说明，一行）→ 学习导航 → 各 KU 小节：**先视频、再截图、再文字**（Closure §7.1 决策）→ 末尾折叠 toggle：Asset Index / QA / Hash。

## 6. 数据库（SQLite）

`channels, channel_prompts, playbooks, sources, runs, stages, model_calls, units, assets, reviews, events, attention, settings`。所有 JSON 产物文件仍为事实来源；DB 存索引与状态，便于 UI 查询。

## 7. YouTube 获取失败分类

`COOKIE_UNREADABLE`（profile 锁/不可读）、`AUTH_REQUIRED`（明确需登录/会员）、`BOT_CHALLENGE`（Sign in to confirm you're not a bot）、`RATE_LIMITED`（429）、`FORMAT_UNAVAILABLE`、`NETWORK`、`UNKNOWN`。只有 `AUTH_REQUIRED` 与持续 `BOT_CHALLENGE`（cookie 读取成功仍失败 ×2）才生成"请通过远程屏幕登录 YouTube"的 Attention。

## 8. 媒体保留（小硬盘）

- 磁盘水位 > 80% 时拒绝开始新 ACQUIRE（Attention）。
- Source 原始媒体：Run 交付后保留 `7 天` 或至 Review 完成 + 2 天（取先到者，可配置）→ `PURGED`（保留 sha256 与元数据，可重新下载）。
- Clips / 截图 / 字幕：保留（发布包的一部分）。Chunks / windows / wav：Stage 完成即删。

## 9. 安全

- Web 只监听 127.0.0.1；另设一个访问 token（首次启动生成，写 data/secrets），浏览器经 cookie 保存——防止同机其他进程误用。
- Notion token 只存 `data/secrets/notion-token`（600），不入库、不入日志。

## 10. 可观测性

每次模型调用：tokens（input/output/thinking/cache）、wall、peak RSS；每 Stage：wall；每 Run：总机器时间、模型时间、媒体时间。金额：`COST_TELEMETRY_UNAVAILABLE`。

## 11. UI 设计方向

- 语气：研究笔记 / 编辑部，信息优先、克制。暖白纸面 + 墨色（浅色）；深墨蓝黑 + 纸色文字（深色）。
- 字体：Display 衬线（Instrument Serif / Newsreader）+ UI 无衬线（Geist / Inter）+ 等宽（Geist Mono / JetBrains Mono）用于时间码与 Ticker；中文回落 PingFang SC / Noto Sans SC。
- 标志性元素：**Source Timeline**（Reader 旁的整段视频时间轴，KU 区段/Clip/截图刻度，与正文滚动联动）；**Stage Rail**（生产阶段的细线轨道，当前阶段微动效）。
- 动效：有意义且克制——状态变化、阶段推进、时间轴联动；尊重 `prefers-reduced-motion`。
- 质量门：每个主要页面截图自审（桌面 1440、笔记本 1280、移动 390；浅/深色），对照获奖站点标准逐项检查排版节奏、层级、对齐、留白、细节状态（空/加载/错误）。

## 12. ADR 候选

1. ADR-1 串行单 Worker + SQLite，而非队列系统（资源约束 + 简单性）。
2. ADR-2 agy CLI 作为唯一 v1 模型通道，接口抽象保留 API Key 路径。
3. ADR-3 结构化输出：`--json-schema` + 本地校验 + ≤1 次无媒体 repair。
4. ADR-4 交付默认 `auto`（先交付后审阅），Channel 可改 `after_approval`。
5. ADR-5 Boundary 自动 sanity 用 ASR 停顿吸附，替代人工核验。
6. ADR-6 Playbook 三步编译（需求抽取 / 编译 / 独立审计）+ 代码级 quote 溯源。
7. ADR-7 时间统一为秒（float）。
8. ADR-8 Web 仅 loopback + SSH 隧道访问。
