// A channel in tuning with made-up data, for looking at the 调试台 without a backend, Notion or model calls.
// Build first (npm run build), then: node web/tools/mock-spread.mjs [port] and open /channels/mock/studio
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";

const STATIC = fileURLToPath(new URL("../../src/assetizer/web/static/", import.meta.url));
const port = Number(process.argv[2] || 8797);
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2", ".woff": "font/woff" };
const now = "2026-11-05T09:00:00+00:00";

const SUBJECTS = [
  ["开场与本期脉络", "主讲人先交代这一期为什么要谈这个问题，以及前几期留下的线索。"],
  ["地方隐性债务的来龙去脉", "把城投平台、土地财政和表外融资串成一条线，说明债务如何在二十年里累积。"],
  ["与九十年代日本的对照", "比较两边的利率路径、人口结构和不良资产处置节奏，指出相似和不相似的地方。"],
  ["债务置换的代价", "置换降低了利息压力，但把久期拉长，也把风险留给了未来的财政。"],
  ["家庭资产负债表", "房价下行之后，家庭把收入优先用于还贷，消费和投资意愿同时走弱。"],
  ["内需与购买力平价", "用购买力平价重新看内需规模，并讨论外贸顺差扩大的原因。"],
  ["周期判断与政策底", "工业化、城市化同时接近尾声，叠加风险意味着政策底不等于市场底。"],
  ["观众问答", "财政、汇率、投资与养老金四个问题，每个问题给出一个可以长期跟踪的指标。"],
];
const DUR = 2307;
const unit = (run, i, start, end, title) => ({ id: `${run}-u${i}`, title, start, end, learning_priority: 1 + (i % 3), tickers: [], priority: 1, notes: i % 4 === 0 ? 1 : 0 });
const cuts = (n) => Array.from({ length: n }, (_, i) => [Math.round((DUR * i) / n), Math.round((DUR * (i + 1)) / n)]);
// v3 splits one section, v2 is the base, v1 has one fewer stretch and merges two
const UNITS = {
  "run-v3": cuts(8).map(([a, b], i) => unit("run-v3", i, a, b, SUBJECTS[i][0])),
  "run-v2": cuts(7).map(([a, b], i) => unit("run-v2", i, a, b, SUBJECTS[i][0])),
  "run-v1": cuts(5).map(([a, b], i) => unit("run-v1", i, a, b, SUBJECTS[i * 1][0])),
  "run-b-v3": cuts(4).map(([a, b], i) => unit("run-b-v3", i, a, b, SUBJECTS[i][0])),
};
const VERSION = { "run-v3": 3, "run-v2": 2, "run-v1": 1, "run-b-v3": 3 };
const run = (id, version, pb, extra = {}) => ({
  id, production: "COMPLETE", stage: null, wait_reason: null, error_class: null, created_at: now, finished_at: now, playbook_id: pb, version, playbook_status: "DRAFT",
  progress: [8, 8], units: UNITS[id]?.length ?? 0, must: 2, clips: 3, notes: 1, chars: 9000, flag_at: [300], eta_seconds: null, error_line: null,
  track: (UNITS[id] || []).map((u) => [u.start, u.end, u.learning_priority]), ...extra,
});
const SAMPLES = [
  { id: "sample-a", title: "谈谈债务问题（模拟）", url: "https://www.youtube.com/watch?v=mock-a", duration: DUR, published_at: "2026-11-02T14:54:00+00:00", added_at: now, current_run_id: "run-v3",
    adopt: { run_id: "run-v3", pinned: false }, delivery: "NONE", notion_url: null, stale: false, busy: false, runs: [run("run-v3", 3, 3), run("run-v2", 2, 2), run("run-v1", 1, 1)] },
  { id: "sample-b", title: "只跑过一版的样片（模拟）", url: "https://www.youtube.com/watch?v=mock-b", duration: 1200, published_at: "2026-10-20T14:54:00+00:00", added_at: now, current_run_id: "run-b-v3",
    adopt: { run_id: "run-b-v3", pinned: false }, delivery: "NONE", notion_url: null, stale: false, busy: false, runs: [run("run-b-v3", 3, 3)] },
];
const playbook = (id, version, status) => ({ id, prompt_id: id, version, status, origin: "COMPILED", created_at: now, activated_at: null, facets: [], audit: null,
  rules: [{ id: "r1", label: "保留逻辑衔接", rule: "保留主讲人论证中的逻辑衔接。", clause_ids: ["c1"] }, { id: "r2", label: `第 ${version} 版的规则`, rule: "术语第一次出现时给出解释。", clause_ids: ["c2"] }] });
const PBS = [playbook(3, 3, "DRAFT"), playbook(2, 2, "RETIRED"), playbook(1, 1, "RETIRED")];
const CHANNEL = {
  id: "mock", name: "模拟频道", stage: "TUNING", mode: "TRACKED", youtube_channel_id: "UCmock", why: "看长期逻辑", playbooks: PBS, sources: 2, last_source: now,
  prompts: PBS.map((p) => ({ id: p.id, version: p.version, note: null, created_at: now, text: "保留逻辑衔接。\n术语第一次出现时给出解释。" })),
  ingest: { enabled: false, min_minutes: 15, scans: [] }, notion: { configured: true, database_title: "模拟知识库", template: "chapters", test_copy: false },
  samples: 2, samples_busy: 0, compile: { state: "done" }, pending_playbook: null, blocked: 0, held: 0,
  stats: { issues: 0, month: 0, last_month: 0, units: 0, clips: 0, seconds: 0, notes: 0, tokens: 0, delivered: 0, registered: 0, failed: 0, waiting: 0, running: 0, queued: 0, weeks: Array(16).fill(0), week_start: "2026-07-20", last: null },
};
const WB = {
  channel: { id: "mock", name: "模拟频道", stage: "TUNING", mode: "TRACKED", youtube_channel_id: "UCmock" }, draft_id: 3, active_id: null, samples: SAMPLES, queue: [],
  playbooks: PBS.map((p) => ({ id: p.id, version: p.version, status: p.status, origin: "COMPILED", created_at: now, activated_at: null, prompt_version: p.version, note: `第 ${p.version} 版`, adopt_count: p.version === 3 ? 2 : 0, missing: p.version === 3 ? 0 : 1, est_minutes: 12 })),
};
const rows = (r) => [
  { id: "t", name: "标题", type: "title", writable: true, mapping: { name: "标题", type: "title", source: "template" }, problem: null, colors: {}, value: "谈谈债务问题" },
  { id: "d", name: "内容发布日期", type: "date", writable: true, mapping: { name: "内容发布日期", type: "date", source: "published" }, problem: null, colors: {}, value: "2026-11-02" },
  { id: "c", name: "内容所属", type: "select", writable: true, mapping: { name: "内容所属", type: "select", source: "channel" }, problem: null, colors: { 模拟频道: "red" }, value: "模拟频道" },
  { id: "u", name: "参考链接", type: "url", writable: true, mapping: { name: "参考链接", type: "url", source: "url" }, problem: null, colors: {}, value: "https://www.youtube.com/watch?v=mock-a" },
  { id: "a", name: "会员/公开", type: "select", writable: true, mapping: { name: "会员/公开", type: "select", source: "access" }, problem: null, colors: { 会员直播: "yellow" }, value: "会员直播" },
  { id: "v", name: "规程版本", type: "rich_text", writable: true, mapping: { name: "规程版本", type: "rich_text", source: "version" }, problem: null, colors: {}, value: `v${VERSION[r]}` },
  { id: "e", name: "最后编辑日期", type: "last_edited_time", writable: false, mapping: null, problem: null, colors: {} },
];
const para = (key, t, ids) => ({ key, type: "paragraph", text: [{ t }], origin: "unit", unit_ids: ids, tpl: false });
function preview(id) {
  const blocks = [
    { key: "intro", type: "callout", emoji: "📌", text: [{ t: `这一版（v${VERSION[id]}）共 ${UNITS[id].length} 个知识单元，全部内容为模拟数据。` }], origin: "tpl", unit_ids: [], tpl: true },
  ];
  UNITS[id].forEach((u, i) => {
    const ids = [u.id], [, lead] = SUBJECTS[i] || SUBJECTS[0], k = `${u.id}`;
    blocks.push({ key: `${k}:h`, type: "heading_2", text: [{ t: u.title }], origin: "unit", unit_ids: ids, tpl: false });
    blocks.push(para(`${k}:p0`, lead, ids));
    const n = 3 + ((i + VERSION[id]) % 4);
    for (let j = 0; j < n; j++) blocks.push(para(`${k}:p${j + 1}`, `第 ${j + 1} 段（v${VERSION[id]}）：${lead}这一段继续展开论证，引用具体的年份和数字，再回到主线，方便比较两个版本的写法和篇幅差异。`, ids));
    if (i % 2 === 0) blocks.push({ key: `${k}:img`, type: "image", caption: `${u.title} · 截图`, asset_id: i + 1, origin: "unit", unit_ids: ids, tpl: false });
    blocks.push({ key: `${k}:li`, type: "bulleted_list_item", text: [{ t: `要点：${u.title}` }], origin: "unit", unit_ids: ids, tpl: false });
  });
  return {
    configured: true, database: { id: "db", url: "https://example.com", title: "模拟知识库" }, source: { id: "sample-a", title: "谈谈债务问题", url: "" }, run_id: id,
    title: [{ text: "谈谈债务问题（模拟）" }], heading: [], icon: null, properties: rows(id), problems: {}, warnings: {}, blocks, body_error: null, context: {},
  };
}
function compare(a, b) {
  const A = UNITS[a], B = UNITS[b], ov = (x, y) => Math.max(0, Math.min(x.end, y.end) - Math.max(x.start, y.start)), span = (x) => x.end - x.start;
  const nb = B.map((u) => { const best = A.find((x) => ov(x, u) / span(u) >= 0.6 && ov(x, u) / span(x) >= 0.6); return best ? "same" : A.find((x) => ov(x, u) / span(u) >= 0.6) ? "split" : "new"; });
  const na = A.map((u) => { const kids = B.filter((x) => ov(u, x) / span(x) >= 0.6); return kids.length >= 2 ? "split" : kids.length === 1 ? "same" : "gone"; });
  const pairs = [], used = new Set();
  for (const x of A) {
    const best = B.map((y, i) => [ov(x, y) / Math.min(span(x), span(y)), i]).filter(([, i]) => !used.has(i)).sort((p, q) => q[0] - p[0])[0];
    if (best && best[0] >= 0.5) { used.add(best[1]); pairs.push({ a: x, b: B[best[1]] }); } else pairs.push({ a: x, b: null });
  }
  B.forEach((y, i) => { if (!used.has(i)) pairs.push({ a: null, b: y }); });
  return { a: { id: a }, b: { id: b }, pairs, matched: pairs.filter((p) => p.a && p.b).length, a_units: A.map((u, i) => ({ ...u, change: na[i] })), b_units: B.map((u, i) => ({ ...u, change: nb[i] })),
    counts: { new: nb.filter((x) => x === "new").length, split: nb.filter((x) => x === "split").length, merge: 0, gone: na.filter((x) => x === "gone").length }, rules_added: [], rules_removed: [], rules_changed: [] };
}
const notion = {
  configured: true, database: { id: "db", url: "https://example.com", title: "模拟知识库" }, config: { template: "chapters", heading: "", icon: "none" }, problems: 0, warnings: {}, page_titles: {}, rev: 1,
  rows: rows("run-v3").map((r) => ({ ...r, options: Object.keys(r.colors), sources: [r.mapping?.source].filter(Boolean), sample: r.value })),
  templates: { chapters: "章节" }, source_labels: { template: "模板", published: "发布日期", channel: "频道", url: "视频链接", access: "会员/公开", version: "规程版本" }, template_vars: { title: "标题" }, access_labels: {},
};
const annotations = (id) => {
  const p = PBS.find((x) => x.id === id) || PBS[0];
  return { playbook: { id: p.id, version: p.version, status: p.status, origin: p.origin }, prompt: { id: p.id, version: p.version, note: null, text: "保留逻辑衔接。\n术语第一次出现时给出解释。" }, has_provenance: true, has_labels: true,
    clauses: [{ id: "c1", text: "保留逻辑衔接。", section: "", start: 0, end: 7, disposition: "RULE", rule_id: "r1", reason: null }, { id: "c2", text: "术语第一次出现时给出解释。", section: "", start: 8, end: 21, disposition: "RULE", rule_id: "r2", reason: null }],
    rules: p.rules, facets: [], article_style: null, conflicts: [], coverage: { clauses: 2, used: 2, system: 0, redundant: 0, ignored: 0 }, vs_previous: null };
};
const detail = (sid, rid) => ({ source: { id: sid, title: "谈谈债务问题（模拟）", url: "https://www.youtube.com/watch?v=mock-a", duration: DUR, published_at: "2026-11-02T14:54:00+00:00", channel_title: "模拟频道", info: {} }, run: { id: rid }, units: UNITS[rid] || [], assets: [], sections: [], flags: [], playback: false });
const shot = (n) => `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="hsl(${n * 47},18%,82%)"/><text x="320" y="190" font-size="28" text-anchor="middle" fill="#555">模拟截图 ${n}</text></svg>`;

function api(url, body) {
  const p = url.pathname, m = (re) => p.match(re);
  if (p === "/api/channels") return { channels: [CHANNEL], universal: null, decisions: [] };
  if (p === "/api/status") return { alerts: [], running: [], queue: [], counts: {} };
  if (p === "/api/channels/mock/workbench") return WB;
  if (p === "/api/channels/mock/notion") return notion;
  if (p === "/api/channels/mock/notion/preview") return UNITS[body?.run_id] ? preview(body.run_id) : { configured: true, empty: true };
  if (p === "/api/compare") return compare(url.searchParams.get("a"), url.searchParams.get("b"));
  if (m(/^\/api\/playbooks\/(\d+)\/annotations$/)) return annotations(Number(m(/(\d+)/)[1]));
  if (m(/^\/api\/sources\/([^/]+)$/)) return detail(decodeURIComponent(m(/^\/api\/sources\/([^/]+)$/)[1]), url.searchParams.get("run"));
  if (m(/^\/api\/sources\/([^/]+)\/adopt$/)) {  // picks the version in memory, so 采用 can be tried
    const s = SAMPLES.find((x) => x.id === decodeURIComponent(m(/^\/api\/sources\/([^/]+)\/adopt$/)[1]));
    if (s) s.adopt = { run_id: body.run_id ?? s.runs[0].id, pinned: !!body.pinned };
    return { ok: true };
  }
  return {};
}

createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  let raw = "";
  for await (const c of req) raw += c;
  if (url.pathname.startsWith("/api/")) {
    const data = api(url, raw ? JSON.parse(raw) : null);
    await new Promise((r) => setTimeout(r, url.pathname.endsWith("/preview") ? 220 : 40));  // a little of the real wait, so loading shows
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(JSON.stringify(data));
  }
  if (url.pathname.startsWith("/media/") || url.pathname.startsWith("/api/media")) {
    res.writeHead(200, { "content-type": "image/svg+xml" });
    return res.end(shot(Number(url.pathname.match(/(\d+)/)?.[1] || 1)));
  }
  try {
    const file = await readFile(join(STATIC, url.pathname));
    res.writeHead(200, { "content-type": TYPES[extname(url.pathname)] || "application/octet-stream" });
    res.end(file);
  } catch {
    res.writeHead(200, { "content-type": "text/html" });
    res.end(await readFile(join(STATIC, "index.html")));
  }
}).listen(port, "127.0.0.1", () => console.log(`http://127.0.0.1:${port}/channels/mock/studio`));
