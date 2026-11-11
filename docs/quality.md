# 质量检查

只列仓库里真实可运行的检查。

## CI

每个 PR（不论往哪个分支开）和推到 main 的提交，都在 devbox 上的 Woodpecker（`https://ci.example.com`）跑 `.woodpecker/quality.yml`，结果以 `ci/woodpecker/pr/quality`、`ci/woodpecker/push/quality` 显示在 GitHub 上。三步并行：

| 步骤 | 做什么 |
|---|---|
| `backend` | 装 ffmpeg，按 `docker/requirements.lock` 的版本装 `.[dev]`，跑 `.venv/bin/pytest -q`（`scripts/ci/backend.sh`） |
| `web` | `npm ci` 后跑 `npm run build`（`scripts/ci/web.sh`） |
| `hook-tunnel` | `sh scripts/lzc/hook-tunnel-process.test.sh`；提交里还没有这个脚本时跳过 |

全量后端测试交给 CI，本机只跑改到的那块（`-k <关键词>`）。后端的 apt 和 pip 先直连清华镜像，不通再经 devbox 的下载代理连官方源（从 devbox 连官方源太慢），前端的 npm 先走 devbox 的 npm 镜像、不通再直连；每个阶段的耗时打在 `CI_CACHE_PHASE` 行里。读失败步骤的日志用 woodpecker-devbox 仓库的 `scripts/read-woodpecker-pipeline.sh Rmosser/assetizer <pipeline> <step>`。

## 环境

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # 生产用的 whisper 另装 '.[asr]'
npm --prefix web ci
```

新工作树没有 `.venv` 时，可以复用另一份检出的环境，把下面的 `.venv/bin/pytest` 换成 `PYTHONPATH=src ../assetizer/.venv/bin/python -m pytest`。

## 每次改动

| 检查 | 命令 | 说明 |
|---|---|---|
| 后端测试 | `.venv/bin/pytest -q` | 在 `tests/test_core.py`，专题在 `tests/test_topics.py`（替身模型走完整的读与综合，不下载、不调模型）和 `tests/test_topic_notion.py`（专题存到 Notion，内存里的假 Notion）；`home` fixture 给每个测试一个隔离的数据库；Notion 调用全部打桩（`FakeNotion`），不会写真实 Notion |
| 定向测试 | `.venv/bin/pytest -q -k <关键词>` | 改哪块先跑哪块 |
| 前端类型检查 + 构建 | `cd web && npm run build` | `tsc -b` 加 `vite build`，产物写入 `src/assetizer/web/static`（不进 Git） |
| 前端工具函数 | `cd web && npm run test:util` | 中文数字（期数）和失败原因的标签；不用 build，也不用浏览器 |
| 选片扩展 | `cd web && npm run test:extension` | 先 `npm run build`，再 `ASSETIZER_HOME=$(mktemp -d) .venv/bin/assetizer web --port 8771`（本机的 8770 被系统的隔空投送占着）；用 Playwright 自带的 Chrome for Testing 加载 `extension/`，在真实 YouTube（未登录）上勾、按 `x`、看侧边栏，先交给不应答的盒子（看「没交出去」、篮子不动），再交给本地实例建专题。不调模型；YouTube 给无头浏览器的可能是旧卡片组件，两种都认 |

仓库没有配置 lint / format 工具；代码里的 `# noqa` 是给 ruff 规则看的，按现有风格写即可。

## 界面改动

先 `npm run build`，再起一个空的本地实例看页面：

```bash
export ASSETIZER_HOME=$(mktemp -d)
.venv/bin/python -m assetizer.cli init           # 种入通用规程和两个频道（seeds/）
.venv/bin/python -m assetizer.cli web --host 127.0.0.1 --port 8799
```

本地实例没有样片和运行，也没有 Notion token，适合看空状态和布局。要看真实数据，把包装到 devbox，再经 SSH 隧道打开容器里的界面（见 [operations.md](operations.md)）。

至少在 1600、1280、390 三种宽度和深色模式下各看一遍。频道页的检查要点见 [channel-page.md](channel-page.md) 第 11 节。主导航五个标签，320、360、414、640 宽都要一行放下、不出横向滚动。设置页还要看 320 宽（名字不折行），以及有红灯、黄灯和全新未配置的样子：在 Playwright 里 `page.route("**/api/settings", …)` 改真实返回最快。

专题的三页要有读完的期才有内容：本地空实例只看得到空状态和选片的布局，选片读链接要本机装有 yt-dlp（没有时每行报「取不到视频信息」），也可以照前面的办法改 `/api/topics/peek` 的返回。重点看出处标签和紧跟的标点不拆行、900 宽以下回答和视频分成两个标签、手机上播放器播放时吸顶、深色下出处标签的对比度。

调试台（矩阵、三栏、展开并排）可以用模拟数据看，不要后端、Notion 和模型（先 build）：

```bash
node web/tools/mock-spread.mjs   # 打开 http://127.0.0.1:8797/channels/mock/studio
```

定稿单和页面图标也能在这里试：点「定稿 v3」看留意、重跑和底栏，页面图标选「频道头像」看预览、保存和还差几页。

机房的额度、设置页的分工与重试可以在本机走完整流程，不碰生产、不调模型：`scripts/dev/magpie_sim.py` 起一个假 magpie（接口形状照实测）、真的网页和真的 worker，数据在 `var/magpie-sim`。控制台能切「连不上、Key 受限、Key 失效、账号掉线」和 CookieCloud 的几种情况（正常、用上一份、用了很久、读不到、上一份也失效），调额度、改勾选、印一期，排补录、连印 8 期、造失败，让内容安全过滤拦下文章（看重试、换模型、失败）。agy、下载、媒体处理和模型的回答是假的，路由、回执、暂停恢复、告警、统计和上报都走真代码。

```bash
npm --prefix web run build
.venv/bin/python scripts/dev/magpie_sim.py          # 机房 http://127.0.0.1:8799/engine，设置 /settings，控制台 http://127.0.0.1:8798；--reset 从头来
```

CookieCloud 配置面板有一个浏览器冒烟脚本（先 build；它在仓库根目录起一个一次性的本地后端，不接受生产地址；默认用本机 Chrome，`BROWSER=webkit` 换 WebKit）。`PYTHON` 要给绝对路径：

```bash
cd web && PYTHON="$(git rev-parse --show-toplevel)/.venv/bin/python" npm run test:cookiecloud
```

## 涉及交付或 Notion 的改动

- 单元测试用 `FakeNotion`，并断言没有意外的写请求。
- 真实验证不在正式库上试：用 Notion 测试副本（先把它加进所用 integration 的 Connections，再在频道页「换库」），或在放好 token 的 devbox 上验证。
- 交付相关的行为以 Notion 读回为准：页面创建成功不代表交付完成，要核对属性、正文块和文件。

## 发版

`sh scripts/lzc/release.sh` 会 lint 安装包；安装后的验收步骤见 [operations.md](operations.md)。
