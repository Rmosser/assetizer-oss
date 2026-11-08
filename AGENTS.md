# 课代表（代号 assetizer）· Agent 入口

把长期关注的投资 / 金融长视频，按频道的学习目标自动拆成知识单元，生成文章、截图、视频片段、字幕、Notion 页面和发布素材包。用户只在频道调试期参与（试跑样片、改学习目标、定稿）；定稿后每期自动写入 Notion。生产运行在懒猫微服 prodbox。

先读 [docs/index.md](docs/index.md)，按任务找对应文档；读够了就停。

## 仓库里有什么

| 路径 | 内容 |
|---|---|
| `src/assetizer/` | 后端：FastAPI（`web/app.py`）、生产流水线（`pipeline/`）、编译（`compiler.py`）、调试与定稿（`tuning.py`）、模型调用（`llm.py`、`providers/`）、Notion 交付（`delivery.py`、`notion/`、`preview.py`）、专题（`topics/`、`web/topics.py`） |
| `web/` | 前端（React + Vite + TS）；构建产物写入 `src/assetizer/web/static`，不进 Git |
| `tests/test_core.py`、`tests/test_topics.py` | 全部后端测试（专题在后者） |
| `seeds/` | 两个频道的初始学习目标与规程（`assetizer init` 使用；只在源码环境有，LPK 不含） |
| `assets/` | 字幕用的中文字体；代码按源码目录的相对路径找它，打包时与 `src/` 放在一起 |
| `experiments/` | 2026-10-05 对照实验的一次性脚本，不参与运行 |
| `package.yml`、`lzc-*.yml`、`docker/`、`scripts/lzc/` | 懒猫 LPK 打包与发版 |
| `deploy/` | 迁移到懒猫之前的 OVH 部署方式，OVH 退役后删除 |
| `docs/` | 当前文档；`docs/history/` 是历史记录，默认不读 |
| `var/` | 本机运行数据（含 `secrets/`），被忽略；不要读取凭据，不要提交 |

## 检查

```bash
.venv/bin/pytest -q          # 后端
cd web && npm run build      # 前端类型检查 + 构建
```

界面、交付和发版的验证方法见 [docs/quality.md](docs/quality.md)。

## 高后果规则

- **凭据与数据**：真实 token、cookie、会员原文、运行数据库和私有媒体不进 Git；日志与页面不显示凭据。Notion token 等凭据只能由用户本人放置（见 [operations.md](docs/operations.md)），Agent 不读取、不复制凭据文件。
- **生产盒子**：在 prodbox 上安装、升级、改设置、试跑、定稿都是对生产的真实操作，需要用户明确授权；升级前先看有没有正在跑的运行并备份数据库。`lzc-cli box switch` 改的是本机全局默认盒子，用完切回 prodbox。
- **Notion**：正式库「知识领袖长期追踪库」是用户在用的知识库。交付和定稿不在正式库上试：用测试副本（先把它加进所用 integration 的 Connections，再在频道页「换库」），或在放好 token 的 devbox 上验证；不覆盖、不归档用户自建的页（定稿只替换课代表写过的页）；交付是否完成以 Notion 读回为准。
- **内容原则**：视频是唯一内容事实来源，见 `src/assetizer/contracts/policy.md`。改提示词或编译逻辑时不能削弱它。
- **模型调用有成本**：编译、重新编译、试跑都会调用模型，不要为了验证随手触发；需要时先说明。

## 改动约定

- 频道页（`web/src/pages/Channel.tsx`、`web/src/components/channel/`）改动前读 [docs/channel-page.md](docs/channel-page.md)：页面级类名用 `ch-` 前缀，复刻区块里的短类名一律写在 `.chp` 之下，避开 `styles.css` 的全局类名；高亮用墨色，朱红只用于冲突、失败、挡住交付的问题、校注刻度和「定」章；JS 与 CSS 的断点一起改。
- 界面文案少字：布局已经说明的事不加次级标签和说明文字。
- 改变长期决定时更新 [docs/design.md](docs/design.md)，保留条目编号（代码注释会引用「design §R2-5」这样的编号）。
- 改了行为就同步更新 README 与相关 docs；当前文档写现状，过程与验证写在提交或 PR 里；需求单和方案落地后移进 `docs/history/`，现状另写进当前文档。提交信息用中文：标题一句话说明改了什么，正文列要点；发版提交写「版本 x.y.z：…」。
- 交付前审阅自己的净差异，集中修复确认的问题；检查通过、目标满足、没有实质阻断就停，不为一般建议继续扩大改动。
- 改 agy、yt-dlp 的适配之前，或周一日报写「日志多了」时，按 [operations.md](docs/operations.md)「日志与迭代」只读扫一遍生产日志。
