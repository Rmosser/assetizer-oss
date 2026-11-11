// YouTube 页面上的勾：
// - 已在篮子里的：缩略图外一圈墨色描边 + 左上角实心圆勾，画在卡片自己的缩略图链接上（属性 + 伪元素，不往 YouTube 的节点里塞东西）。
// - 悬停勾：鼠标在缩略图上时出现。YouTube 的悬停预览是 ytd-app > #video-preview 下的全局层、和缩略图重合，
//   挂在卡片里的勾会被它盖住，所以悬停勾画在扩展自己的 fixed 浮层里（Shadow DOM，最上层）。
// - x：勾选或取消鼠标下的那期；没有就作用于正在看的视频。在输入框里不触发。
// - 观看页：标题一行右端一个同样的圆勾。
// - 自检：页面上有 /watch 链接却一张卡片都没认出来时告诉后台，角标变灰「?」。
// 扩展重载后旧页面里的脚本失效（Extension context invalidated）：撤掉自己的一切。
(() => {
  const AZ = globalThis.AZ;
  const yt = AZ.sites.youtube;
  if (document.getElementById("az-basket")) return; // injected twice

  const alive = () => { try { return !!chrome.runtime?.id; } catch { return false; } };
  let keys = new Set();
  let hovered = null;      // the card the pointer is on (kept while it is on that card's preview)
  let healthy = null;

  const CHECK = (stroke) => `url("data:image/svg+xml,${encodeURIComponent(
    `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='${stroke}' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'><path d='M6 12.5l4 4 8-9'/></svg>`)}")`;

  // ------------------------------------------------------------------ marks on the cards
  const style = document.createElement("style");
  style.id = "az-basket-style";
  style.textContent = `
    [data-az-on] { position: relative; outline: 2px solid #1f1e1b; outline-offset: 2px; border-radius: 12px; }
    html[dark] [data-az-on] { outline-color: #e9e3d6; }
    ytd-watch-next-secondary-results-renderer [data-az-on], ytd-playlist-panel-video-renderer [data-az-on] { border-radius: 8px; }
    [data-az-on]::after {
      content: ""; position: absolute; z-index: 3; top: 8px; left: 8px; width: 24px; height: 24px; border-radius: 50%;
      background: #f3efe6 ${CHECK("#1f1e1b")} center / 15px no-repeat; box-shadow: 0 1px 4px rgba(0,0,0,.4); pointer-events: none;
    }
    ytd-watch-metadata #title.az-room h1 { padding-right: 40px; }
    ytd-watch-metadata #title.az-room { position: relative; }
  `;
  document.documentElement.append(style);

  // ------------------------------------------------------------------ the floating check (hover) and the watch page's
  const host = document.createElement("div");
  host.id = "az-basket";
  host.style.cssText = "position:fixed;inset:0 auto auto 0;width:0;height:0;z-index:2147483646;";
  const root = host.attachShadow({ mode: "closed" });
  root.innerHTML = `<style>
    .ck { position: fixed; width: 32px; height: 32px; padding: 0; margin: 0; border: 0; background: none; cursor: pointer; display: none; }
    .ck.show { display: block; }
    .ck::before { content: ""; position: absolute; inset: 4px; border-radius: 50%; box-sizing: border-box;
      border: 2px solid #fff; background: rgba(15,14,12,.28); box-shadow: 0 1px 4px rgba(0,0,0,.45); transition: transform .12s; }
    .ck:hover::before { transform: scale(1.08); }
    .ck.on::before { border: 0; background: #f3efe6 ${CHECK("#1f1e1b")} center / 15px no-repeat; }
    .ck:focus-visible { outline: 2px solid #fff; outline-offset: 1px; border-radius: 50%; }
  </style><button class="ck" type="button" title="选片篮 (x)" aria-label="加入选片篮"></button>`;
  const hover = root.querySelector(".ck");
  document.documentElement.append(host);

  // the watch page's check sits in the title row, its own element in a shadow root
  const wHost = document.createElement("span");
  wHost.style.cssText = "position:absolute;right:0;top:50%;transform:translateY(-50%);";
  const wRoot = wHost.attachShadow({ mode: "closed" });
  wRoot.innerHTML = `<style>
    button { width: 32px; height: 32px; padding: 0; border: 0; border-radius: 50%; background: none; cursor: pointer; position: relative; display: block; }
    button::before { content: ""; position: absolute; inset: 5px; border-radius: 50%; box-sizing: border-box; border: 1.5px solid currentColor; opacity: .55; }
    button:hover::before { opacity: 1; }
    button.on::before { opacity: 1; border: 0; background: #1f1e1b ${CHECK("#f3efe6")} center / 14px no-repeat; }
    :host(.dk) button.on::before { background: #e9e3d6 ${CHECK("#1f1e1b")} center / 14px no-repeat; }
  </style><button type="button" title="选片篮 (x)" aria-label="加入选片篮"></button>`;
  const wBtn = wRoot.querySelector("button");

  const dark = () => document.documentElement.hasAttribute("dark");
  function paintWatch() {
    const here = yt.current();
    const title = document.querySelector("ytd-watch-metadata #title");
    if (!here || !title) { wHost.remove(); return; }
    if (wHost.parentNode !== title) { title.classList.add("az-room"); title.append(wHost); }
    const on = keys.has(here.key);
    wBtn.classList.toggle("on", on);
    wBtn.setAttribute("aria-label", on ? "移出选片篮" : "加入选片篮");
    wBtn.setAttribute("aria-pressed", String(on));
    // on the page itself (not on a picture): ink on paper in the light theme, paper on ink in the dark
    wHost.classList.toggle("dk", dark());
    wBtn.style.color = dark() ? "#f1f1f1" : "#0f0f0f";
  }
  wBtn.addEventListener("click", (e) => { e.stopPropagation(); const here = yt.current(); if (here) toggle(here); });

  // ------------------------------------------------------------------ painting
  function paint() {
    if (!alive()) return teardown();
    const seen = new Set();
    let read = 0;
    for (const card of document.querySelectorAll(yt.CARD)) {
      const a = yt.mount(card);
      const id = a && yt.idOf(a.getAttribute("href"));
      if (!id) continue;
      read++;
      seen.add(a);
      const on = keys.has(`yt:${id}`);
      if (on !== a.hasAttribute("data-az-on")) a.toggleAttribute("data-az-on", on);
    }
    for (const a of document.querySelectorAll("[data-az-on]")) if (!seen.has(a)) a.removeAttribute("data-az-on");
    paintWatch();
    place();
    // 自检：页面上有视频链接、却一张卡片都没认出来
    const links = document.querySelectorAll('a[href*="/watch?v="]').length;
    const ok = !(links >= 5 && read === 0);
    if (ok !== healthy) { healthy = ok; try { chrome.runtime.sendMessage({ type: "health", ok }); } catch { /* reloaded */ } }
  }
  let soon = 0;
  const later = () => { clearTimeout(soon); soon = setTimeout(paint, 150); };

  // ------------------------------------------------------------------ hovering
  function place() {
    const a = hovered && yt.mount(hovered);
    if (!a || !a.isConnected || document.fullscreenElement) { hover.classList.remove("show"); return; }
    const r = a.getBoundingClientRect();
    if (r.width < 60 || r.bottom < 0 || r.top > innerHeight) { hover.classList.remove("show"); return; }
    const id = yt.idOf(a.getAttribute("href"));
    const on = keys.has(`yt:${id}`);
    hover.style.left = `${r.left + 4}px`;
    hover.style.top = `${r.top + 4}px`;
    hover.classList.toggle("on", on);
    hover.setAttribute("aria-label", on ? "移出选片篮" : "加入选片篮");
    hover.classList.add("show");
  }
  let frame = 0, lastTarget = null;
  function track(e) {
    lastTarget = e.target;
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const t = lastTarget;
      if (!(t instanceof Element) || t === host) return;
      if (t.closest("#video-preview, ytd-video-preview")) return; // on the preview of the card hovered: keep it
      const card = t.closest(yt.CARD);
      const a = card && yt.mount(card);
      const next = a && a.contains(t) && yt.read(card) ? card : null;
      if (next !== hovered) { hovered = next; place(); }
    });
  }

  // ------------------------------------------------------------------ toggling
  async function toggle(item) {
    if (!alive()) return teardown();
    await AZ.basket.toggle(item);
  }
  hover.addEventListener("click", (e) => {
    e.preventDefault(); e.stopPropagation();
    const item = hovered && yt.read(hovered);
    if (item) toggle(item);
  });
  // a click on the floating check must not reach YouTube's card under it
  for (const k of ["mousedown", "pointerdown", "mouseup"]) hover.addEventListener(k, (e) => e.stopPropagation());

  const editable = (e) => e.composedPath().some((n) => n instanceof Element
    && (n.matches("input, textarea, select, [contenteditable=''], [contenteditable='true']") || n.isContentEditable));
  function key(e) {
    if (e.key !== "x" || e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || editable(e)) return;
    const item = (hovered && yt.read(hovered)) || yt.current();
    if (!item) return;
    e.preventDefault(); e.stopPropagation();
    toggle(item);
  }

  // ------------------------------------------------------------------ messages (the side panel asks what this page is)
  function answer(msg, _sender, reply) {
    if (!alive()) return false;
    if (msg?.type === "current") reply(yt.current());
    else if (msg?.type === "playlist") reply(yt.playlist());
    return false;
  }

  // ------------------------------------------------------------------ wiring
  const mo = new MutationObserver(later);
  const onScroll = () => { if (hovered) place(); };
  const onNav = () => { hovered = null; later(); };
  const onLeave = () => { hovered = null; place(); };
  function teardown() {
    mo.disconnect();
    removeEventListener("pointermove", track, true);
    removeEventListener("keydown", key, true);
    removeEventListener("scroll", onScroll, true);
    removeEventListener("yt-navigate-finish", onNav);
    document.removeEventListener("mouseleave", onLeave);
    host.remove(); wHost.remove(); style.remove();
    for (const a of document.querySelectorAll("[data-az-on]")) a.removeAttribute("data-az-on");
    document.querySelector("ytd-watch-metadata #title.az-room")?.classList.remove("az-room");
  }

  AZ.basket.all().then((list) => {
    keys = new Set(list.map((x) => x.key));
    paint();
    mo.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["href", "dark"] });
    new MutationObserver(later).observe(document.documentElement, { attributes: true, attributeFilter: ["dark"] });
  });
  AZ.basket.onChange((list) => { keys = new Set(list.map((x) => x.key)); paint(); });
  addEventListener("pointermove", track, { capture: true, passive: true });
  addEventListener("keydown", key, true);
  addEventListener("scroll", onScroll, { capture: true, passive: true });
  addEventListener("yt-navigate-finish", onNav);
  document.addEventListener("mouseleave", onLeave);
  chrome.runtime.onMessage.addListener(answer);
})();
