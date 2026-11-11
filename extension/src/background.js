// 后台：工具栏角标（篮子里几期；适配器认不出卡片时是灰色「?」）、右键菜单「加入选片篮」、交给课代表。
importScripts("boxes.js", "basket.js");
const AZ = globalThis.AZ;

const INK = "#1f1e1b";
const GREY = "#8d877b";
const LINKS = ["https://www.youtube.com/watch?*", "https://www.youtube.com/shorts/*", "https://youtu.be/*", "https://m.youtube.com/watch?*"];

// ------------------------------------------------------------------ 角标
const broken = new Set(); // 认不出卡片的标签页
async function badge(tabId) {
  const n = (await AZ.basket.all()).length;
  const text = n ? String(n > 99 ? "99+" : n) : "";
  await chrome.action.setBadgeBackgroundColor({ color: INK });
  await chrome.action.setBadgeText({ text });
  for (const id of broken) {
    try {
      await chrome.action.setBadgeText({ tabId: id, text: "?" });
      await chrome.action.setBadgeBackgroundColor({ tabId: id, color: GREY });
    } catch { broken.delete(id); }
  }
  if (tabId != null && !broken.has(tabId)) {
    try { await chrome.action.setBadgeText({ tabId, text }); await chrome.action.setBadgeBackgroundColor({ tabId, color: INK }); } catch { /* closed */ }
  }
}
AZ.basket.onChange(() => badge());
chrome.tabs.onRemoved.addListener((id) => broken.delete(id));

// ------------------------------------------------------------------ 安装、启动
function setup() {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "add-link", title: "加入选片篮", contexts: ["link"], targetUrlPatterns: LINKS });
    chrome.contextMenus.create({ id: "add-page", title: "加入选片篮", contexts: ["page", "video"], documentUrlPatterns: LINKS });
  });
  badge();
}
chrome.runtime.onInstalled.addListener(setup);
chrome.runtime.onStartup.addListener(setup);

// ------------------------------------------------------------------ 右键菜单：只看链接，不依赖 YouTube 的页面结构
function idOf(href) {
  try {
    const u = new URL(href);
    const v = u.hostname === "youtu.be" ? u.pathname.slice(1)
      : u.pathname === "/watch" ? u.searchParams.get("v")
      : (u.pathname.match(/^\/shorts\/([^/?#]+)/) || [])[1];
    return v && /^[A-Za-z0-9_-]{11}$/.test(v) ? v : null;
  } catch { return null; }
}

// 标题和频道从 oEmbed 取（不要登录、不花额度）；时长取不到，交接后课代表核对时补上
async function describe(id) {
  try {
    const r = await fetch(`https://www.youtube.com/oembed?format=json&url=${encodeURIComponent(`https://www.youtube.com/watch?v=${id}`)}`);
    if (r.ok) { const j = await r.json(); return { title: j.title || null, channel: j.author_name || null }; }
  } catch { /* offline: the ids are enough */ }
  return { title: null, channel: null };
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const id = idOf(info.menuItemId === "add-link" ? info.linkUrl : info.pageUrl);
  if (!id) return;
  const at = { key: `yt:${id}`, site: "youtube", kind: "video", id, url: `https://www.youtube.com/watch?v=${id}`,
    title: null, channel: null, dur: null, thumb: `https://i.ytimg.com/vi/${id}/mqdefault.jpg` };
  // 观看页上：页面自己知道标题和时长
  if (info.menuItemId === "add-page" && tab?.id != null) {
    try {
      const here = await chrome.tabs.sendMessage(tab.id, { type: "current" });
      if (here && here.id === id) { await AZ.basket.add([here]); return; }
    } catch { /* no content script on this page */ }
  }
  await AZ.basket.add([{ ...at, ...(await describe(id)) }]);
});

// ------------------------------------------------------------------ 交给课代表
// 已经开着的课代表标签页（专题页优先）就切过去，让页面上的脚本把这一批交给它；没有就新开 /topics#pick=<批次>
async function hand() {
  const s = await AZ.handoff.send();
  if (!s) return { ok: false };
  const box = await AZ.boxes.current();
  const tabs = await chrome.tabs.query({ url: `${box.origin}/*` });
  const tab = tabs.find((t) => new URL(t.url || box.origin).pathname.startsWith("/topics")) || tabs[0];
  if (tab?.id != null) {
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "offer", batch: s.batch });
      await chrome.tabs.update(tab.id, { active: true });
      await chrome.windows.update(tab.windowId, { focused: true });
      return { ok: true, batch: s.batch };
    } catch { /* the page is from before the extension was loaded: open a new one */ }
  }
  await chrome.tabs.create({ url: `${box.origin}/topics#pick=${s.batch}` });
  return { ok: true, batch: s.batch };
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg?.type === "hand") { hand().then(reply); return true; }
  if (msg?.type === "health" && sender.tab?.id != null) {
    if (msg.ok) broken.delete(sender.tab.id); else broken.add(sender.tab.id);
    badge(sender.tab.id);
  }
  return false;
});
