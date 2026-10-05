import { chromium } from "playwright-core";
// node shootset.mjs <outdir> [base] [only]
const [out, base = "http://localhost:8772", only = ""] = process.argv.slice(2);
const shots = [
  ["home", "/", [0]],
  ["home50", "/?mock=50", [0, 1500]],
  ["reader", "/issue/1MuKAUgL4r4", [0, 1400, "flag", "end"]],
  ["channels", "/channels", [0, 700]],
  ["engine", "/engine", [0, 900]],
];
const sizes = [[1440, 900], [1180, 820], [390, 844]];
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const report = [];
for (const [w, h] of sizes) for (const theme of ["light", "dark"]) {
  if (w === 1180 && theme === "dark") continue;
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: w < 500 ? 2 : 1, colorScheme: theme, hasTouch: w < 500, isMobile: w < 500 });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => report.push(`[${w} ${theme}] pageerror: ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") report.push(`[${w} ${theme}] console: ${m.text()}`); });
  for (const [name, url, positions] of shots) {
    if (only && !name.startsWith(only)) continue;
    if (w === 1180 && name !== "reader") continue;
    await page.goto(base + url, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(3500);
    for (const pos of positions) {
      if (pos === "flag") {
        const ok = await page.evaluate(() => {
          const z = [...document.querySelectorAll(".pi-cards .zhupi, aside.margin .zhupi:not(.info)")].find((e) => e.offsetParent && e.closest("article, aside")?.previousElementSibling);
          const unit = z?.closest("article.unit") || z?.closest("aside")?.previousElementSibling;
          if (!unit) return false;
          unit.scrollIntoView();
          return true;
        });
        if (!ok) continue;
      } else if (pos === "end") {
        await page.evaluate(() => document.querySelector(".end")?.scrollIntoView({ block: "end" }));
      } else await page.evaluate((y) => window.scrollTo(0, y), pos);
      await page.waitForTimeout(1200);
      const metrics = await page.evaluate(() => {
        const overflow = document.documentElement.scrollWidth - innerWidth;
        const small = [...document.querySelectorAll("a,button,summary,input,[role=switch],[role=slider]")]
          .filter((e) => { const r = e.getBoundingClientRect(); return r.width && r.height && r.top >= 0 && r.bottom <= innerHeight && (r.height < 32 || r.width < 32); })
          .map((e) => (e.textContent || e.getAttribute("aria-label") || e.tagName).trim().slice(0, 14));
        const half = /[\p{Script=Han}][,;:()]|[,;:()][\p{Script=Han}]/u.test(document.body.innerText);
        return { overflow, small: small.slice(0, 8), half };
      });
      report.push(`${name}-${pos}-${w}-${theme}: overflow=${metrics.overflow} halfwidth=${metrics.half} small=${JSON.stringify(metrics.small)}`);
      await page.screenshot({ path: `${out}/${name}-${pos}-${w}-${theme}.png` });
    }
  }
  await ctx.close();
}
await browser.close();
console.log(report.join("\n"));
