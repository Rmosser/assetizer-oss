import { chromium } from "playwright-core";
const [out, url, y, w = "1440"] = process.argv.slice(2);
const b = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" });
const mob = +w < 500;
const p = await b.newPage({ viewport: { width: +w, height: mob ? 844 : 900 }, deviceScaleFactor: mob ? 2 : 1 });
await p.goto("http://localhost:8772" + url, { waitUntil: "domcontentloaded" }); await p.waitForTimeout(3000);
await p.evaluate((y) => window.scrollTo(0, y), +y); await p.waitForTimeout(1200);
await p.screenshot({ path: out }); await b.close();
