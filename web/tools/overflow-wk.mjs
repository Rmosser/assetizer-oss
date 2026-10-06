import { webkit, devices } from "playwright-core";
const b = await webkit.launch();
const ctx = await b.newContext({ ...devices["iPhone 15"] });
await ctx.route("**/*", (r) => (r.request().method() === "GET" ? r.continue() : r.fulfill({ json: {} })));
const p = await ctx.newPage();
for (const url of ["/issue/1MuKAUgL4r4", "/"]) {
  await p.goto("http://localhost:8772" + url, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(3500);
  console.log(url, await p.evaluate(() => {
    const W = document.documentElement.clientWidth;
    const out = [];
    for (const e of document.querySelectorAll("body *")) {
      const r = e.getBoundingClientRect();
      const cs = getComputedStyle(e);
      const textOver = cs.overflowX === "visible" && e.scrollWidth > e.clientWidth + 1 && e.clientWidth > 0 && r.left + e.scrollWidth > W + 0.5;
      if ((r.width && r.right > W + 0.5) || textOver) out.push(`${e.tagName.toLowerCase()}.${(e.className || "").toString().slice(0, 30)} right=${r.right.toFixed(1)} W=${W} text=${(e.textContent || "").slice(0, 20)}`);
    }
    return { scrollW: document.documentElement.scrollWidth, W, top: out.slice(0, 8) };
  }));
}
await b.close();
