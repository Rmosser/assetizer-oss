import { chromium } from "playwright-core";
const out = process.argv[2];
const pages = (process.argv[3] || "home,reader,channels,engine").split(",");
const themes = (process.argv[4] || "light").split(",");
const width = +(process.argv[5] || 1440);
const urls = { home: "/", reader: "/issue/1MuKAUgL4r4", channels: "/channels", engine: "/engine" };
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
for (const theme of themes) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 1, colorScheme: theme });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("PAGEERROR", e.message));
  page.on("console", (m) => m.type() === "error" && console.log("CONSOLE", m.text()));
  for (const p of pages) {
    await page.goto("http://127.0.0.1:8771" + urls[p], { waitUntil: "networkidle" });
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${out}/${p}-${theme}-${width}.png`, fullPage: false });
    if (p === "reader") {
      const el = await page.$("article.unit + aside.margin .zhupi:not(.info)");
      if (el) { await el.evaluate((e) => e.parentElement.previousElementSibling.scrollIntoView()); await page.waitForTimeout(700);
        await page.screenshot({ path: `${out}/${p}-${theme}-${width}-flag.png` }); }
      await page.evaluate(() => window.scrollTo(0, 2600));
      await page.waitForTimeout(700);
      await page.screenshot({ path: `${out}/${p}-${theme}-${width}-mid.png` });
    }
  }
  await ctx.close();
}
await browser.close();
