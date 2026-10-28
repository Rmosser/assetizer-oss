# 文档地图

从上往下按需读，读够了就停：

1. 根目录 [AGENTS.md](../AGENTS.md)：仓库是什么、检查命令、高后果规则（每次任务都适用）。
2. 本页：按问题找到下面的当前文档。
3. 当前文档：只描述现状，改代码时一起改。
4. 代码与模块 docstring：细节和参数以代码为准。
5. [history/](history/)：当时的记录，默认不读，只在追溯「为什么」时翻。

## 按问题找

| 想知道 / 要做 | 先读 | 代码入口 |
|---|---|---|
| 怎么用、人要做哪些事 | [README.md](../README.md) | — |
| 系统由哪些部分组成、数据放在哪 | [architecture.md](architecture.md) | `cli.py`、`config.py`、`db.py` |
| 某一期生产失败、某个阶段的行为 | [architecture.md](architecture.md)「生产流水线」 | `pipeline/runner.py`（阶段表与调度）、`pipeline/*.py` |
| 模型调用、提示词、结构化输出 | [design.md](design.md) §R-1、§R-16、§R-17 | `llm.py`、`providers/agy.py`、`contracts/prompts.py` |
| 知识单元、文章、截图、片段、字幕 | [design.md](design.md) §R-2～§R-9 | `pipeline/semantic.py`、`pipeline/media_stages.py`、`media/` |
| 学习目标编译、规程、稿纸批注、重新编译 | [channel-page.md](channel-page.md) §3.4 | `compiler.py`、`web/app.py`（重新编译与编译状态） |
| 频道调试：样片、试跑、采用、对比、定稿 | [channel-page.md](channel-page.md) §3 | `tuning.py`、`pipeline/runner.py`（跑完自动采用）、`web/app.py`（定稿单查库）、`delivery.py` |
| 频道页的界面、点亮与连线、断点、样式坑 | [channel-page.md](channel-page.md) | `web/src/pages/Channel.tsx`、`web/src/components/channel/`、`web/src/channel.css` |
| Notion 交付、映射、页面模板、查重、预览 | [architecture.md](architecture.md)「交付到 Notion」、[channel-page.md](channel-page.md) §4 | `delivery.py`、`notion/`、`templates.py`、`preview.py` |
| 自动收录新视频、立即扫描、补录旧视频 | [architecture.md](architecture.md)「频道生命周期」、[design.md](design.md) §R6-5、[channel-page.md](channel-page.md) §2 | `ingest.py`、`web/app.py`（看板接口）、`web/src/components/channel/Board.tsx` |
| agy 在网页里登录、额度（5 小时 / 每周） | [operations.md](operations.md)「凭据与登录」、[design.md](design.md) §R6-3、§R6-4 | `providers/agy_login.py`、`providers/agy.py`（`usage`、`record_usage`）、`providers/magpie.py`（`quota_view`）、`web/src/components/AgyLogin.tsx` |
| YouTube 登录与 CookieCloud | [README.md](../README.md)、[cookiecloud-credentials-ui.md](cookiecloud-credentials-ui.md)、[cookiecloud-read-ingress.md](cookiecloud-read-ingress.md) | `cookies.py`、`youtube.py` |
| 告警与 Telegram 通知 | [README.md](../README.md)「通知」 | `notify.py`、`pipeline/runner.py`（告警） |
| 其他页面：刊、对照视图、机房，统计口径 | [architecture.md](architecture.md)「前端」、[design.md](design.md) §R6-1、§R6-2 | `web/src/pages/`、`web/src/components/Figures.tsx`、`web/app.py`（`/api/engine/stats`） |
| 为什么这样设计（长期决定与理由） | [design.md](design.md) | — |
| 内容原则：视频是唯一事实来源 | [`contracts/policy.md`](../src/assetizer/contracts/policy.md) | — |
| 改完怎么检查 | [quality.md](quality.md) | `tests/test_core.py` |
| 发版、升级、回滚、凭据、登录、测试副本、直接看容器 | [operations.md](operations.md) | `scripts/lzc/`、`lzc-manifest.yml` |
| 哪些复杂度是给模型和工具的限制打的补丁、每周基准怎么跑 | [benchmarks.md](benchmarks.md) | `src/assetizer/bench/` |

表里的代码路径都相对 `src/assetizer/`；以 `web/`、`tests/`、`scripts/` 开头的路径和 `lzc-manifest.yml` 相对仓库根目录。

## 历史记录（docs/history/）

| 文档 | 是什么 |
|---|---|
| [design-v0.2.md](history/design-v0.2.md) | v0.2 设计与裁决全文；现行条目已整理进 design.md |
| [lazycat-lpk-plan.md](history/lazycat-lpk-plan.md) | 迁移到懒猫前的打包方案（已实施） |
| [design-review.md](history/design-review.md) | v0.1 设计的独立评审（2026-10-05） |
| [code-review.md](history/code-review.md) | 首版代码的独立评审（2026-10-05，提交 `3bb533f`） |
| [ui-review-round1.md](history/ui-review-round1.md)、[ui-review-round2.md](history/ui-review-round2.md) | 首版界面的两轮评审（2026-10-06） |
| [experiments-2026-10-05.md](history/experiments-2026-10-05.md) | 媒体条件变化后的对照实验（design.md §R-18 的依据） |
| [cookiecloud-credentials-review.md](history/cookiecloud-credentials-review.md) | CookieCloud 凭据界面的审查（2026-10-13） |
| [cookiecloud-read-tunnel-530.md](history/cookiecloud-read-tunnel-530.md) | CookieCloud 只读入口间歇性 530 / 502 的问题报告（2026-10-22，OVH 时期） |
