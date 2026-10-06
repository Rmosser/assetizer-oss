import { webkit, chromium } from "playwright-core";
for (const [name, bt, opts] of [["webkit", webkit, {}], ["chromium", chromium, { executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" }]]) {
  const b = await bt.launch(opts); const p = await b.newPage();
  await p.setContent(`<html><body style="font-feature-settings:'chws' 1"><p id=a style="font:20px serif">x</p><p id=b class=c>x</p></body></html>`);
  console.log(name, await p.evaluate(() => [getComputedStyle(document.getElementById("a")).fontFeatureSettings, getComputedStyle(document.getElementById("b")).fontFeatureSettings]));
  await b.close();
}
