// Renders the 课 seal into PNG app icons (run once: node tools/icons.mjs)
import { chromium } from "playwright-core";
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
for (const [size, pad, name] of [[180, 0.1, "seal-180.png"], [192, 0.1, "seal-192.png"], [512, 0.1, "seal-512.png"], [512, 0.2, "seal-maskable-512.png"]]) {
  const page = await browser.newPage({ viewport: { width: size, height: size } });
  const inner = Math.round(size * (1 - 2 * pad)), off = Math.round(size * pad);
  await page.setContent(`<!doctype html><html style="margin:0;overflow:hidden"><body style="margin:0;width:${size}px;height:${size}px;background:#f3efe6;position:relative;overflow:hidden">
    <div style="position:absolute;left:${off}px;top:${off}px;width:${inner}px;height:${inner}px;background:#b8322a;border-radius:${Math.round(inner * 0.05)}px;transform:rotate(-3deg);
      box-shadow: inset 0 0 0 ${Math.max(2, Math.round(inner * 0.035))}px rgba(243,239,230,.22);display:flex;align-items:center;justify-content:center">
      <span style="font:700 ${Math.round(inner * 0.62)}px/1 'Songti SC','Noto Serif SC',serif;color:#f3efe6">课</span>
    </div></body></html>`);
  await page.screenshot({ path: `public/${name}`, clip: { x: 0, y: 0, width: size, height: size } });
  await page.close();
}
await browser.close();
