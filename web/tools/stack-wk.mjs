import { webkit, devices } from "playwright-core";
const b = await webkit.launch();
const ctx = await b.newContext({ ...devices["iPhone 15"] });
await ctx.route("**/*", (r) => (r.request().method() === "GET" ? r.continue() : r.fulfill({ json: {} })));
const p = await ctx.newPage();
await p.goto("http://localhost:8772/issue/1MuKAUgL4r4", { waitUntil: "domcontentloaded" });
await p.waitForTimeout(3500);
const variant = process.argv[3] || "asis";
if (variant === "nocv") await p.addStyleTag({ content: ".unit{content-visibility:visible!important}" });
if (variant === "noblur") await p.addStyleTag({ content: ".reviewbar,.masthead{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}" });
if (variant === "solid") await p.addStyleTag({ content: ".reviewbar{background:var(--paper)!important}" });
if (variant === "nograin") await p.addStyleTag({ content: "body::before{display:none!important}" });
await p.evaluate(() => window.scrollTo(0, 2600)); await p.waitForTimeout(400);
await p.evaluate(() => window.scrollBy(0, 300)); await p.waitForTimeout(1200);
console.log(await p.evaluate(() => {
  const rb = document.querySelector(".reviewbar"); const r = rb.getBoundingClientRect();
  const hits = [10, 25, 40].map((y) => { const e = document.elementFromPoint(120, r.top + y); return e ? `${e.tagName}.${e.className}` : null; });
  const cs = getComputedStyle(rb);
  return { rect: [r.top, r.bottom], bg: cs.backgroundColor, bf: cs.backdropFilter || cs.webkitBackdropFilter, z: cs.zIndex, pos: cs.position, hits,
    unitCV: getComputedStyle(document.querySelector(".unit")).contentVisibility };
}));
await p.screenshot({ path: process.argv[2] + `/wk-stack-${variant}.png`, clip: { x: 0, y: 0, width: 393, height: 140 } });
await b.close();
