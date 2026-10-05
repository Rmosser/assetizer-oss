import { chromium } from "playwright-core";
const [out, base] = [process.argv[2], process.argv[3] || "http://localhost:8772"];
const shots = [
  ["home", "/", [0]],
  ["reader", "/issue/1MuKAUgL4r4", [0, 1400, "flag"]],
  ["channels", "/channels", [0, 700]],
  ["engine", "/engine", [0, 900]],
];
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
for (const [w, h] of [[1440, 900], [390, 844]]) for (const theme of ["light", "dark"]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: w < 500 ? 2 : 1, colorScheme: theme });
  const page = await ctx.newPage();
  for (const [name, url, positions] of shots) {
    await page.goto(base + url, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(3000);
    for (const pos of positions) {
      if (pos === "flag") {
        const el = await page.$("article.unit + aside.margin .zhupi:not(.info)");
        if (!el) continue;
        await el.evaluate((e) => e.parentElement.previousElementSibling.scrollIntoView());
      } else await page.evaluate((y) => window.scrollTo(0, y), pos);
      await page.waitForTimeout(900);
      await page.screenshot({ path: `${out}/${name}-${pos}-${w}-${theme}.png` });
    }
  }
  await ctx.close();
}
await browser.close();
