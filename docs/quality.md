# 质量检查

只列仓库里真实可运行的检查。没有 CI；提交前在本机跑。

## 环境

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # 生产用的 whisper 另装 '.[asr]'
npm --prefix web ci
```

新工作树没有 `.venv` 时，可以复用另一份检出的环境，把下面的 `.venv/bin/pytest` 换成 `PYTHONPATH=src ../assetizer/.venv/bin/python -m pytest`。

## 每次改动

| 检查 | 命令 | 说明 |
|---|---|---|
| 后端测试 | `.venv/bin/pytest -q` | 全部在 `tests/test_core.py`；`home` fixture 给每个测试一个隔离的数据库；Notion 调用全部打桩（`FakeNotion`），不会写真实 Notion |
| 定向测试 | `.venv/bin/pytest -q -k <关键词>` | 改哪块先跑哪块 |
| 前端类型检查 + 构建 | `cd web && npm run build` | `tsc -b` 加 `vite build`，产物写入 `src/assetizer/web/static`（不进 Git） |

仓库没有配置 lint / format 工具；代码里的 `# noqa` 是给 ruff 规则看的，按现有风格写即可。

## 界面改动

先 `npm run build`，再起一个空的本地实例看页面：

```bash
export ASSETIZER_HOME=$(mktemp -d)
.venv/bin/python -m assetizer.cli init           # 种入通用规程和两个频道（seeds/）
.venv/bin/python -m assetizer.cli web --host 127.0.0.1 --port 8799
```

本地实例没有样片和运行，也没有 Notion token，适合看空状态和布局。要看真实数据，把包装到 devbox，再经 SSH 隧道打开容器里的界面（见 [operations.md](operations.md)）。

至少在 1600、1280、390 三种宽度和深色模式下各看一遍。频道页的检查要点见 [channel-page.md](channel-page.md) 第 11 节。

调试台（矩阵、三栏、展开并排）可以用模拟数据看，不要后端、Notion 和模型（先 build）：

```bash
node web/tools/mock-spread.mjs   # 打开 http://127.0.0.1:8797/channels/mock/studio
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
