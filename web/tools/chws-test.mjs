import { webkit, chromium, devices } from "playwright-core";
for (const [name, bt, opts] of [["webkit", webkit, {}], ["chromium", chromium, { executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" }]]) {
  const b = await bt.launch(opts);
  const ctx = await b.newContext({ ...devices["iPhone 15"] });
  await ctx.route("**/*", (r) => (r.request().method() === "GET" ? r.continue() : r.fulfill({ json: {} })));
  const p = await ctx.newPage();
  await p.goto("http://localhost:8772/issue/1MuKAUgL4r4", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(3500);
  const r = await p.evaluate(async () => {
    await document.fonts.ready;
    const mk = (css) => { const s = document.createElement("span"); s.style.cssText = `font:400 20px var(--serif);white-space:nowrap;${css}`; s.textContent = "说“可以”，（括号）。「引」、"; document.body.append(s); const w = s.getBoundingClientRect().width; s.remove(); return Math.round(w * 10) / 10; };
    return { plain: mk("text-spacing-trim:space-all"), chws: mk("font-feature-settings:'chws' 1;text-spacing-trim:space-all"), trim: mk("text-spacing-trim:trim-start"), trimChws: mk("text-spacing-trim:trim-start;font-feature-settings:'chws' 1"), punctLoaded: document.fonts.check("400 20px 'AZ Punct'", "，") };
  });
  console.log(name, JSON.stringify(r));
  await b.close();
}
