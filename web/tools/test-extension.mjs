// The 选片篮 extension (../extension) in a real Chromium, on the real YouTube (not signed in), handing over to a local
// 课代表 on http://localhost:8771 (run `ASSETIZER_HOME=<scratch> assetizer web --port 8771` first; no worker, no model).
//   node tools/test-extension.mjs            SMOKE_ARTIFACTS=<dir> to keep screenshots
// What it proves: the hover check works over YouTube's preview, x, the watch page's check, the side panel's list and
// sum, a box that does not answer (没交出去, the basket kept, the same batch handed again), and the handover: the 新专题
// sheet opens with the basket as one group, the topic is made, the basket empties.
import { chromium } from "playwright-core";
import { mkdtempSync, existsSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join, resolve } from "node:path";
import assert from "node:assert/strict";

const EXT = resolve(import.meta.dirname, "../../extension");
const CHROMIUM = process.env.CHROMIUM
  || join(homedir(), "Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing");
const OUT = process.env.SMOKE_ARTIFACTS;
const shot = async (page, name) => OUT && page.screenshot({ path: join(OUT, `extension-${name}.png`) });
if (!existsSync(CHROMIUM)) throw new Error(`no Chromium at ${CHROMIUM} (set CHROMIUM)`);

const ctx = await chromium.launchPersistentContext(mkdtempSync(join(tmpdir(), "az-ext-")), {
  executablePath: CHROMIUM, headless: process.env.HEADED ? false : true, viewport: { width: 1440, height: 900 }, locale: "zh-CN",
  args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
});
try {
  const sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker");
  const id = new URL(sw.url()).host;
  await sw.evaluate(() => chrome.storage.local.set({ box: "local-api" }));
  const basket = () => sw.evaluate(() => chrome.storage.local.get("basket").then((r) => r.basket || []));

  // ---------------------------------------------------------------- search results
  const yt = await ctx.newPage();
  await yt.goto("https://www.youtube.com/results?search_query=resin+3d+print+supports");
  // YouTube serves either card (2026-11: the new lockup to a signed-in Chrome, the old renderer to headless Chrome)
  const thumbs = yt.locator(["yt-lockup-view-model a.ytLockupViewModelContentImage", "ytd-video-renderer a#thumbnail"]
    .map((s) => `ytd-item-section-renderer ${s}[href*='/watch?v=']`).join(", "));
  await thumbs.nth(3).waitFor({ timeout: 30000 });
  const at = async (i) => { await thumbs.nth(i).scrollIntoViewIfNeeded(); return thumbs.nth(i).boundingBox(); };

  let b = await at(0);
  await yt.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await yt.waitForTimeout(300);
  await yt.mouse.click(b.x + 20, b.y + 20);       // the hover check, top left
  await yt.waitForTimeout(300);
  assert.match(yt.url(), /results\?/, "the check must not open the video");
  assert.equal((await basket()).length, 1);
  assert.equal(await thumbs.nth(0).getAttribute("data-az-on"), "", "a ticked card is marked");
  const first = (await basket())[0];
  assert.ok(first.title && first.dur > 0 && first.channel, `read title, length, channel: ${JSON.stringify(first)}`);

  b = await at(1);
  await yt.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await yt.waitForTimeout(400);
  await yt.keyboard.press("x");
  await yt.waitForTimeout(300);
  assert.equal((await basket()).length, 2, "x ticks the card under the pointer");

  // over YouTube's own preview (a layer of its own on top of the thumbnail): the check is still there to click
  b = await at(2);
  await yt.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await yt.waitForTimeout(2500);
  const preview = await yt.evaluate(() => !!document.querySelector("ytd-video-preview:not([hidden])"));
  await yt.mouse.move(b.x + 22, b.y + 22);
  await yt.mouse.click(b.x + 20, b.y + 20);
  await yt.waitForTimeout(300);
  assert.equal((await basket()).length, 3, `the check over the preview (preview showing: ${preview})`);
  await yt.mouse.click(b.x + 20, b.y + 20);
  await yt.waitForTimeout(300);
  assert.equal((await basket()).length, 2, "clicked again: out");

  // typing x in the search box is typing
  await yt.click("input[name='search_query'], input#search");
  await yt.keyboard.press("x");
  assert.equal((await basket()).length, 2, "x in a text field is not a tick");
  await yt.evaluate(() => scrollTo(0, 0));
  b = await at(0);
  await yt.mouse.move(b.x + b.width / 2, b.y + b.height / 2);   // the hover check on a ticked card
  await yt.waitForTimeout(600);
  await shot(yt, "search");

  // ---------------------------------------------------------------- watch page
  // a video of the results not in the basket
  const inBasket = new Set((await basket()).map((x) => x.id));
  const hrefs = await thumbs.evaluateAll((as) => as.map((a) => new URL(a.href).searchParams.get("v")));
  const other = hrefs.find((v) => v && !inBasket.has(v));
  await yt.goto(`https://www.youtube.com/watch?v=${other}`);
  await yt.waitForSelector("ytd-watch-metadata h1", { timeout: 30000 });
  await yt.waitForTimeout(1500);
  await yt.mouse.move(5, 5);
  await yt.keyboard.press("x");
  await yt.waitForTimeout(500);
  const list = await basket();
  assert.ok(list.some((x) => x.id === other), "x on the watch page takes the video being watched");
  await shot(yt, "watch");

  // ---------------------------------------------------------------- the side panel
  const panel = await ctx.newPage();
  await panel.setViewportSize({ width: 380, height: 760 });
  await panel.goto(`chrome-extension://${id}/panel.html`);
  await panel.waitForSelector(".it");
  await panel.waitForFunction(() => [...document.images].every((i) => i.complete), null, { timeout: 10000 }).catch(() => {});
  const rows = await panel.locator(".it").count();
  assert.equal(rows, list.length);
  const sum = await panel.locator("#sum").textContent();
  assert.match(sum, new RegExp(`^${list.length} 期 · `), sum);
  await shot(panel, "panel");
  await panel.locator(".it").first().hover();
  await panel.locator(".it .x").first().click();
  assert.equal((await basket()).length, list.length - 1, "× takes it out");

  // ---------------------------------------------------------------- a box that does not answer
  await sw.evaluate(() => chrome.storage.local.set({ box: "local" }));   // nothing on :5173
  const kept = (await basket()).length;
  const dead = ctx.waitForEvent("page");
  await panel.click("#hand");
  const nowhere = await dead;
  await panel.waitForFunction(() => document.querySelector("#sum")?.textContent === "没交出去：连不上课代表", null, { timeout: 25000 });
  assert.equal((await basket()).length, kept, "the basket stays");
  assert.equal(await panel.locator(".it.sent").count(), 0, "nothing is 已送出 that no page took up");
  const batch = await sw.evaluate(() => chrome.storage.local.get("sent").then((r) => r.sent.batch));
  await nowhere.close();
  await sw.evaluate(() => chrome.storage.local.set({ box: "local-api" }));

  // ---------------------------------------------------------------- handing over
  const opened = ctx.waitForEvent("page");
  await panel.click("#hand");
  assert.equal(await sw.evaluate(() => chrome.storage.local.get("sent").then((r) => r.sent.batch)), batch,
    "handed again: the same batch, so never two topics");
  const az = await opened;
  await az.waitForLoadState();
  await az.waitForSelector(".tp-sheet .tp-fd-h", { timeout: 15000 });
  assert.equal(new URL(az.url()).hash, "", "the batch id leaves the address");
  const head = await az.locator(".tp-sheet .tp-fd-h .tp-fd-l").first().textContent();
  assert.equal(head, "选片篮");
  const ticked = await az.locator(".tp-sheet .tp-pk input:checked").count();
  assert.equal(ticked, list.length - 1, "the basket's videos are ticked at once");
  await az.waitForTimeout(800);   // the sheet's opening
  await shot(az, "sheet");
  await az.fill(".tp-sheet .tp-q", "树脂打印怎么加支撑");
  await az.click(".tp-sheet button[type=submit]");
  await az.waitForURL(/\/topics\/\d+$/, { timeout: 15000 });
  await az.waitForTimeout(500);
  assert.equal((await basket()).length, 0, "what went into the topic left the basket");
  assert.equal(await sw.evaluate(() => chrome.storage.local.get("sent").then((r) => r.sent ?? null)), null);
  console.log("extension: hover check, check over the preview, x, watch page, side panel, box not answering, handover passed");
} finally {
  await ctx.close();
}
