import { chromium } from "playwright-core";
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const page = await browser.newPage();
await page.setContent(`<html lang="zh-CN"><body style="font:20px serif">
<span id=a style="text-autospace:normal">MRNA双重EMA10均线</span><br>
<span id=b style="text-autospace:no-autospace">MRNA双重EMA10均线</span><br>
<span id=c style="text-spacing-trim:trim-start">（括号</span><span id=d style="text-spacing-trim:space-all">（括号</span>
</body></html>`);
console.log(await page.evaluate(() => ({
  autospace: CSS.supports("text-autospace", "normal"), trim: CSS.supports("text-spacing-trim", "trim-start"),
  wa: document.getElementById("a").getBoundingClientRect().width, wb: document.getElementById("b").getBoundingClientRect().width,
  wc: document.getElementById("c").getBoundingClientRect().width, wd: document.getElementById("d").getBoundingClientRect().width,
  ua: navigator.userAgent,
})));
await browser.close();
