// Archive compact rows + 翻开 page turn: geometry, transition lifecycle, reduced motion, WebKit. Read-only:
// non-GET requests are intercepted; the 50-issue home is a mock (the real issue cloned, dates spread 4 days apart).
import { chromium, webkit, devices } from "playwright-core";
const [out, base = "http://localhost:8772"] = process.argv.slice(2);
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DAY = 86400e3;
const ymd = (t) => new Date(t).toISOString().slice(0, 10).replace(/-/g, "");
const report = {};

async function prepare(ctx, n = 50) {
  await ctx.route("**/*", (r) => (r.request().method() === "GET" ? r.continue() : r.fulfill({ json: {} })));
  await ctx.route("**/api/overview", async (route) => {
    const r = await route.fetch(); const j = await r.json();
    const real = j.issues[0], now = Date.now();
    j.issues = Array.from({ length: n }, (_, i) => {
      const t = now - (2 + i * 4) * DAY, sealed = i % 5 === 3;
      return { ...real, no: n - i, published_at: ymd(t), updated_at: new Date(t).toISOString(), review: sealed ? "APPROVED" : real.review, flags: sealed ? 0 : real.flags };
    });
    await route.fulfill({ response: r, json: j });
  });
  // record every view transition and how it ended
  await ctx.addInitScript(() => {
    window.__vt = [];
    const orig = Document.prototype.startViewTransition;
    if (!orig) return;
    Document.prototype.startViewTransition = function (cb) {
      const rec = { at: performance.now() }; window.__vt.push(rec);
      const t = orig.call(this, cb);
      t.updateCallbackDone.then(() => { rec.cbDone = true; rec.yAfterSwap = scrollY; }, (e) => { rec.cbErr = String(e); });
      t.ready.then(() => { rec.ready = true; }, (e) => { rec.readyErr = String(e); });
      t.finished.then(() => { rec.ms = Math.round(performance.now() - rec.at); }, (e) => { rec.finErr = String(e); });
      return t;
    };
  });
}

const geometry = (page) => page.evaluate(() => {
  const rows = [...document.querySelectorAll(".toc-row")];
  const h = (el) => Math.round(el.getBoundingClientRect().height);
  const compact = rows.filter((r) => r.classList.contains("compact")), full = rows.filter((r) => !r.classList.contains("compact"));
  const lastFull = full.at(-1), firstCompact = compact[0];
  return {
    rows: rows.length, compact: compact.length,
    compactH: compact.length ? [Math.min(...compact.map(h)), Math.max(...compact.map(h))] : null,
    fullH: full.length ? [Math.min(...full.map(h)), Math.max(...full.map(h))] : null,
    compactCoverVisible: compact.filter((r) => getComputedStyle(r.querySelector(".cover")).display !== "none").length,
    compactMeta: firstCompact?.querySelector(".meta")?.textContent,
    boundary: [lastFull?.querySelector(".meta")?.textContent, firstCompact?.querySelector(".meta")?.textContent],
    truncated: compact.filter((r) => { const t = r.querySelector("h3"); return t.scrollWidth > t.clientWidth + 1; }).length,
    stateOverflow: compact.filter((r) => r.querySelector(".toc-state").getBoundingClientRect().right > r.getBoundingClientRect().right + 1).length,
    hOverflow: document.documentElement.scrollWidth - innerWidth,
    pageH: document.documentElement.scrollHeight,
    vtNames: [...document.querySelectorAll("*")].map((e) => getComputedStyle(e).viewTransitionName).filter((n) => n && n !== "none"),
  };
});

async function run(browser, tag, ctxOpts) {
  const errors = [];
  const watch = (page) => {
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (m) => { if (["error", "warning"].includes(m.type())) errors.push(`${m.type()}: ${m.text()}`); });
  };
  const r = (report[tag] = {});
  for (const scheme of ["light", "dark"]) {
    const ctx = await browser.newContext({ ...ctxOpts, colorScheme: scheme });
    await prepare(ctx);
    const page = await ctx.newPage(); watch(page);
    await page.goto(base + "/", { waitUntil: "domcontentloaded" }); await page.waitForTimeout(3500);
    if (scheme === "light") r.home = await geometry(page);
    // the sixty-day boundary in view
    await page.evaluate(() => { const c = document.querySelector(".toc-row.compact"); if (c) scrollTo(0, c.getBoundingClientRect().top + scrollY - innerHeight * 0.45); });
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${out}/${tag}-boundary-${scheme}.png` });
    if (scheme === "light") {
      // 翻开: issue row → reader, then back
      // a row centred on screen first, so the click itself does not scroll
      const idx = 6;
      await page.evaluate((i) => { const r = document.querySelectorAll(".toc-row")[i]; scrollTo(0, r.getBoundingClientRect().top + scrollY - innerHeight / 2); }, idx);
      await page.waitForTimeout(600);
      const y0 = await page.evaluate(() => Math.round(scrollY));
      await page.locator(".toc-row").nth(idx).click();
      await page.waitForTimeout(1500);
      r.toReader = await page.evaluate(() => ({ path: location.pathname, y: scrollY, vt: window.__vt.slice() }));
      // single-line titles must stay on one line even with phrase joints (<wbr>) inside
      r.singleLine = await page.evaluate(() => {
        const long = "MRNA 双缺口地量回踩，<wbr>长周末前主动减仓，<wbr>等待周二开盘确认，<wbr>再决定是否回补";
        const probe = (html) => { const d = document.createElement("div"); d.style.cssText = "position:fixed;left:0;top:0;width:240px"; d.innerHTML = html; document.body.append(d);
          const el = d.querySelector("[data-p]"); const lh = parseFloat(getComputedStyle(el).lineHeight) || 20; const h = el.getBoundingClientRect().height; d.remove(); return Math.round(h / lh * 10) / 10; };
        return {
          dockTitleLines: probe(`<button class="dock-title" style="width:240px"><span class="kw" data-p>${long}</span></button>`),
          bubbleLines: probe(`<div class="sk-bubble" data-p style="position:static;transform:none;max-width:240px"><span class="kw">${long}</span></div>`),
        };
      });
      await page.waitForTimeout(2500);
      await page.screenshot({ path: `${out}/${tag}-reader-after-turn.png` });
      await page.goBack(); await page.waitForTimeout(3000);
      r.back = await page.evaluate((y0) => ({ path: location.pathname, y: Math.round(scrollY), expectedY: y0, vtCount: window.__vt.length }), y0);
      // masthead link → another page (the masthead keeps still under the turn)
      const nav = page.locator(".masthead a[href='/channels'], .tabbar a[href='/channels']").first();
      if (await nav.count()) {
        await nav.click(); await page.waitForTimeout(1500);
        r.toChannels = await page.evaluate(() => ({ path: location.pathname, vt: window.__vt.at(-1), vtNames: [...document.querySelectorAll("*")].map((e) => getComputedStyle(e).viewTransitionName).filter((n) => n && n !== "none") }));
      }
    }
    await ctx.close();
  }
  // mid-turn frames (Chromium only: slow the animation down through CDP)
  if (browser.browserType().name() === "chromium") {
    const ctx = await browser.newContext({ ...ctxOpts, colorScheme: "light" });
    await prepare(ctx);
    const page = await ctx.newPage(); watch(page);
    await page.goto(base + "/", { waitUntil: "domcontentloaded" }); await page.waitForTimeout(3500);
    const cdp = await ctx.newCDPSession(page);
    await cdp.send("Animation.enable"); await cdp.send("Animation.setPlaybackRate", { playbackRate: 0.08 });
    await page.locator(".toc-row").first().click();
    for (const ms of [250, 1500, 3500]) { await page.waitForTimeout(ms === 250 ? 250 : ms - 250); await page.screenshot({ path: `${out}/${tag}-turn-${ms}.png` }); }
    await cdp.send("Animation.setPlaybackRate", { playbackRate: 1 });
    await ctx.close();
  }
  // reduced motion: no transition, navigation still works
  const ctx = await browser.newContext({ ...ctxOpts, reducedMotion: "reduce" });
  await prepare(ctx);
  const page = await ctx.newPage(); watch(page);
  await page.goto(base + "/", { waitUntil: "domcontentloaded" }); await page.waitForTimeout(3500);
  await page.locator(".toc-row").first().click(); await page.waitForTimeout(1500);
  r.reduced = await page.evaluate(() => ({ path: location.pathname, vtCount: window.__vt.length, supported: "startViewTransition" in document }));
  await ctx.close();
  r.errors = [...new Set(errors)];
}

const chrome = await chromium.launch({ executablePath: CHROME, headless: true });
await run(chrome, "c1440", { viewport: { width: 1440, height: 900 } });
await run(chrome, "c390", { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
await chrome.close();
const wk = await webkit.launch();
await run(wk, "wk390", { ...devices["iPhone 15"] });
await wk.close();
const { writeFileSync } = await import("node:fs");
writeFileSync(`${out}/archive-vt.json`, JSON.stringify(report, null, 1));
console.log(JSON.stringify(report, null, 1));
