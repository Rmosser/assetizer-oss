// Interactive / edge states for review. Read-only: approvals and failures are simulated with request interception.
import { chromium } from "playwright-core";
const [out, base = "http://localhost:8772"] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const SRC = "/issue/1MuKAUgL4r4";
const approve = async (ctx) => ctx.route("**/api/sources/1MuKAUgL4r4", async (route) => {
  const r = await route.fetch(); const j = await r.json();
  j.source.review = "APPROVED"; j.source.review_at = "2026-10-06T09:00:00+00:00";
  j.flags = j.flags.map((f) => (f.severity !== "INFO" ? { ...f, resolution: f.id % 3 ? "ACCEPTED" : "DISMISSED" } : f));
  await route.fulfill({ response: r, json: j });
});
const shot = (page, name) => page.screenshot({ path: `${out}/${name}.png` });
const wait = (page, ms = 1500) => page.waitForTimeout(ms);

for (const [w, h, tag] of [[1440, 900, "1440"], [390, 844, "390"]]) {
  const mob = w < 500;
  const mk = async (scheme = "light") => browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: mob ? 2 : 1, colorScheme: scheme, hasTouch: mob, isMobile: mob });
  // approved issue: seal in the head, quiet margin, end block
  let ctx = await mk(); await approve(ctx); let page = await ctx.newPage();
  await page.goto(base + SRC, { waitUntil: "domcontentloaded" }); await wait(page, 3500);
  await shot(page, `st-approved-head-${tag}`);
  await page.evaluate(() => document.querySelector(".end")?.scrollIntoView({ block: "end" })); await wait(page);
  await shot(page, `st-approved-end-${tag}`);
  await ctx.close();

  ctx = await mk(); page = await ctx.newPage();
  await page.goto(base + SRC, { waitUntil: "domcontentloaded" }); await wait(page, 3500);
  if (mob) {
    await page.evaluate(() => window.scrollTo(0, 1600)); await wait(page, 800);
    await page.click(".rb-count"); await wait(page, 900); await shot(page, `st-index-sheet-${tag}`);
    await page.keyboard.press("Escape"); await wait(page, 500);
    await page.click(".rb-acts .btn"); await wait(page, 900); await shot(page, `st-review-sheet-${tag}`);
    await page.keyboard.press("Escape"); await wait(page, 500);
    const card = await page.$(".pi-cards .zhupi h5");
    if (card) { await card.scrollIntoViewIfNeeded(); await page.evaluate(() => window.scrollBy(0, -120)); await card.click(); await wait(page, 900); await shot(page, `st-card-open-${tag}`); }
  } else {
    await page.keyboard.press("n"); await wait(page, 1800); await shot(page, `st-step1-${tag}`);
    await page.keyboard.press("n"); await wait(page, 1800); await shot(page, `st-step2-${tag}`);
  }
  // lightbox
  const sb = await page.$(".shot-btn");
  if (sb) { await sb.scrollIntoViewIfNeeded(); await sb.click(); await wait(page, 1200); await shot(page, `st-lightbox-${tag}`); await page.keyboard.press("Escape"); await wait(page, 500); }
  // dock: play a clip, scroll away
  const play = await page.$(".film .play");
  if (play) {
    await play.scrollIntoViewIfNeeded(); await play.click(); await wait(page, 2500);
    await page.evaluate(() => window.scrollBy(0, 1400)); await wait(page, 2000);
    await shot(page, `st-dock-${tag}`);
  }
  await ctx.close();

  // fault: Access session expired → login HTML instead of JSON
  ctx = await mk(); await ctx.route("**/api/overview", (r) => r.fulfill({ status: 200, contentType: "text/html", body: "<html>login</html>" }));
  page = await ctx.newPage(); await page.goto(base + "/", { waitUntil: "domcontentloaded" }); await wait(page, 2500);
  await shot(page, `st-fault-${tag}`); await ctx.close();
  // empty
  ctx = await mk(); await ctx.route("**/api/overview", async (route) => { const r = await route.fetch(); const j = await r.json(); j.issues = []; j.decisions = []; j.open_flags = 0; await route.fulfill({ response: r, json: j }); });
  page = await ctx.newPage(); await page.goto(base + "/", { waitUntil: "domcontentloaded" }); await wait(page, 2500);
  await shot(page, `st-empty-${tag}`); await ctx.close();
}
await browser.close();
console.log("done");
