// iPhone emulation on WebKit (Safari's engine): feature support, autospace, chrome hide, screenshots. Read-only.
import { webkit, devices } from "playwright-core";
const [out, base = "http://localhost:8772"] = process.argv.slice(2);
const browser = await webkit.launch();
const report = {};
for (const theme of ["light", "dark"]) {
  const ctx = await browser.newContext({ ...devices["iPhone 15"], colorScheme: theme });
  await ctx.route("**/*", (r) => (r.request().method() === "GET" ? r.continue() : r.fulfill({ json: {} })));
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(base + "/issue/1MuKAUgL4r4", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(4000);
  if (theme === "light") {
    report.ua = await page.evaluate(() => navigator.userAgent);
    report.support = await page.evaluate(() => Object.fromEntries([
      ["text-autospace", "normal"], ["text-spacing-trim", "trim-start"], ["text-wrap", "balance"], ["text-wrap", "pretty"],
      ["content-visibility", "auto"], ["backdrop-filter", "blur(4px)"], ["-webkit-backdrop-filter", "blur(4px)"],
      ["text-emphasis", "open circle"], ["-webkit-text-emphasis", "open circle"], ["hanging-punctuation", "allow-end"],
      ["line-break", "strict"], ["writing-mode", "vertical-rl"], ["mask-image", "linear-gradient(#000,#0000)"],
    ].map(([p, v]) => [`${p}: ${v}`, CSS.supports(p, v)]).concat([
      [":has()", CSS.supports("selector(:has(a))")], ["Intl.Segmenter", "Segmenter" in Intl],
      ["dialog.showModal", typeof HTMLDialogElement !== "undefined" && "showModal" in HTMLDialogElement.prototype],
      ["color-mix", CSS.supports("color", "color-mix(in srgb, red 50%, blue)")],
    ])));
    // does autospace actually add width? compare the same string with and without
    report.autospaceWidth = await page.evaluate(() => {
      const mk = (v) => { const s = document.createElement("span"); s.style.cssText = `font:20px serif;text-autospace:${v};white-space:nowrap`; s.textContent = "MRNA双重EMA10均线"; document.body.append(s); const w = s.getBoundingClientRect().width; s.remove(); return w; };
      return { normal: mk("normal"), none: mk("no-autospace") };
    });
    report.trimWidth = await page.evaluate(() => {
      const mk = (v) => { const s = document.createElement("span"); s.style.cssText = `font:20px "AZ Punct","Noto Serif SC",serif;text-spacing-trim:${v};white-space:nowrap`; s.textContent = "”，（括号）。"; document.body.append(s); const w = s.getBoundingClientRect().width; s.remove(); return w; };
      return { trimStart: mk("trim-start"), spaceAll: mk("space-all") };
    });
  }
  await page.screenshot({ path: `${out}/wk-reader-0-${theme}.png` });
  // scroll down a bit: masthead should hide, review bar should stick at the top
  await page.evaluate(() => window.scrollTo(0, 2400));
  await page.waitForTimeout(500);
  await page.evaluate(() => window.scrollBy(0, 300));
  await page.waitForTimeout(1200);
  const chrome = await page.evaluate(() => ({
    data: document.documentElement.dataset.chrome,
    masthead: Math.round(document.querySelector(".masthead").getBoundingClientRect().bottom),
    reviewbar: Math.round(document.querySelector(".reviewbar").getBoundingClientRect().top),
    overflow: document.documentElement.scrollWidth - innerWidth,
  }));
  report[`chrome-${theme}`] = chrome;
  await page.screenshot({ path: `${out}/wk-reader-scrolled-${theme}.png` });
  const card = await page.$(".pi-cards .zp-toggle");
  if (card) { await card.scrollIntoViewIfNeeded(); await page.evaluate(() => window.scrollBy(0, -140)); await card.tap(); await page.waitForTimeout(900); await page.screenshot({ path: `${out}/wk-card-${theme}.png` }); }
  await page.evaluate(() => document.querySelector(".end")?.scrollIntoView({ block: "center" }));
  await page.waitForTimeout(1500);
  await page.evaluate(() => document.querySelector(".end")?.scrollIntoView({ block: "center" }));
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${out}/wk-end-${theme}.png` });
  await page.goto(base + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${out}/wk-home-${theme}.png` });
  report[`errors-${theme}`] = errors;
  await ctx.close();
}
await browser.close();
console.log(JSON.stringify(report, null, 1));
