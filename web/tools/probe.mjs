import { chromium } from "playwright-core";
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto("http://localhost:8772/issue/1MuKAUgL4r4", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(4000);
const r = await page.evaluate(() => {
  const q = [...document.querySelectorAll("em.quan")].map((e) => [e.dataset.flag, e.textContent.slice(0, 40)]);
  const h5 = [...document.querySelectorAll(".margin .zhupi:not(.info) h5")].map((e) => e.textContent.slice(0, 50));
  return { quan: q, h5 };
});
console.log(JSON.stringify(r, null, 1));
// step to first flag and screenshot
await page.keyboard.press("n");
await page.waitForTimeout(1500);
await page.screenshot({ path: process.argv[2] + "/step1-1440.png" });
await page.keyboard.press("n");
await page.waitForTimeout(1500);
await page.screenshot({ path: process.argv[2] + "/step2-1440.png" });
await browser.close();
