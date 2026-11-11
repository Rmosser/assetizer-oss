// 侧边栏：选片篮的唯一一份界面。最近加的在最上面；底部合计与「交给课代表」。
const AZ = globalThis.AZ;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

let items = [];
let sent = null;       // the batch handed over and not confirmed yet
let missed = false;    // handed over, and no 课代表 page took it up in CLAIM_WAIT: said until one does, or it is handed again
const CLAIM_WAIT = 15_000;
let undo = null;       // what 清空 took out, for one 撤销
let here = null;       // the playlist the current tab is on

function row(x) {
  const out = !!sent?.claimed && sent.keys.includes(x.key);  // the page has it: 已送出 (till it says what went in)
  const meta = [x.kind === "list" ? (x.count ? `${x.count} 期` : "列表") : x.channel,
    x.dur ? AZ.clock(x.dur) : null, out ? "已送出" : null].filter(Boolean).join(" · ");
  const pic = x.thumb ? `<img src="${esc(x.thumb)}" alt="" loading="lazy">` : `<span class="ph" aria-hidden="true">≡</span>`;
  return `<li class="it${out ? " sent" : ""}">
    <a href="${esc(x.url)}" target="_blank" rel="noreferrer" tabindex="-1">${pic}</a>
    <span class="t"><b>${esc(x.title || x.url.replace(/^https:\/\/www\./, ""))}</b>${meta ? `<small>${esc(meta)}</small>` : ""}</span>
    <button class="x" type="button" data-key="${esc(x.key)}" aria-label="移出"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button>
  </li>`;
}

function render() {
  const shown = [...items].reverse();
  $("items").innerHTML = shown.map(row).join("");
  $("empty").hidden = items.length > 0 || !!undo;
  $("clear").hidden = items.length < 2;
  $("undo").hidden = !undo;
  $("foot").hidden = items.length === 0;
  const videos = items.filter((x) => x.kind === "video");
  const lists = items.filter((x) => x.kind === "list");
  const n = videos.length + lists.reduce((s, x) => s + (x.count || 0), 0);
  const { seconds, unknown } = AZ.total(videos);
  const about = unknown > 0 || lists.length > 0;
  const h = AZ.hours(seconds);
  const lost = missed && !sent?.claimed;
  $("sum").className = lost ? "sum bad" : "sum";
  $("sum").textContent = lost ? "没交出去：连不上课代表" : [`${n} 期`, h ? `${about ? "约 " : ""}${h}` : ""].filter(Boolean).join(" · ");
  const inList = here && items.some((x) => x.key === here.key);
  $("list").hidden = !here || inList;
  if (here && !inList) $("list").innerHTML = `<span>+</span><b>${esc(here.title || "这个列表")}</b>${here.count ? `<span>· ${here.count} 期</span>` : ""}`;
}

async function load() {
  items = await AZ.basket.all();
  sent = await AZ.handoff.pending();
  render();
}

// the tab in front: a playlist offers 「+ 这个列表」
async function look() {
  here = null;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.id != null && /^https:\/\/www\.youtube\.com\//.test(tab.url || "")) {
      here = await chrome.tabs.sendMessage(tab.id, { type: "playlist" });
    }
  } catch { /* not a page with the content script */ }
  render();
}

$("items").addEventListener("click", (e) => {
  const b = e.target.closest(".x");
  if (b) AZ.basket.remove([b.dataset.key]);
});
$("list").addEventListener("click", () => { if (here) AZ.basket.add([here]); });
$("clear").addEventListener("click", async () => {
  undo = items;
  await AZ.basket.clear();
});
$("undo").querySelector("button").addEventListener("click", async () => {
  const back = undo;
  undo = null;
  await chrome.storage.local.set({ basket: back });
});
// handed over: the 课代表 page takes it up once it opens and the box answers (bridge.js → handoff.claim); a box asleep, a
// network down or a sign-in in the way leaves it with no taker. Gone (confirmed) counts as taken.
async function taken(batch) {
  for (const end = Date.now() + CLAIM_WAIT; Date.now() < end;) {
    const s = await AZ.handoff.pending();
    if (!s || s.batch !== batch || s.claimed) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

$("hand").addEventListener("click", async () => {
  $("hand").disabled = true;
  missed = false;
  render();
  try {
    const r = await chrome.runtime.sendMessage({ type: "hand" });
    if (!r?.ok) throw new Error("没交出去");
    missed = !(await taken(r.batch));
  } catch (e) {
    missed = true;
  } finally {
    $("hand").disabled = false;
    sent = await AZ.handoff.pending();
    render();
  }
});

AZ.basket.onChange((list) => {
  // something new in the basket: 清空 cannot be taken back any more
  if (undo && list.length) undo = null;
  items = list;
  render();
});
chrome.storage.onChanged.addListener((ch, area) => {
  if (area === "local" && ch.sent) { sent = ch.sent.newValue && Date.now() - ch.sent.newValue.at < AZ.handoff.WAIT ? ch.sent.newValue : null; render(); }
});
chrome.tabs.onActivated.addListener(look);
chrome.tabs.onUpdated.addListener((_id, info, tab) => { if (tab.active && (info.status === "complete" || info.url)) look(); });
// a sent batch that was never confirmed goes back to being just the basket
setInterval(async () => { const s = await AZ.handoff.pending(); if (!!s !== !!sent) { sent = s; render(); } }, 60_000);

load().then(look);
