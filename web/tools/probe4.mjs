import { chromium } from "playwright-core";
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto("http://localhost:8772/issue/1MuKAUgL4r4", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(3000);
console.log(await page.evaluate(() => {
  const cap = document.querySelector(".head-fig-cap .kw");
  return { body: getComputedStyle(document.body).textAutospace, cap: cap && getComputedStyle(cap).textAutospace, capHTML: cap?.innerHTML.slice(0, 120),
    h1: getComputedStyle(document.querySelector("h1")).textAutospace, lang: document.documentElement.lang, wordBreak: cap && getComputedStyle(cap).wordBreak };
}));
await browser.close();
