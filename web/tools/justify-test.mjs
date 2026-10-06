import { webkit, chromium, devices } from "playwright-core";
const html = (css) => `<html lang="zh-CN"><body style="margin:0;padding:16px;width:358px;font:17px/1.8 'Songti SC',serif;line-break:strict;text-autospace:normal">
<p style="margin:0;${css}">本期技术分析围绕长周末前后的持仓管理、板块分化背景下的标的筛选以及关键结构位的交易计划展开。主讲人系统复盘了当前持仓的应对逻辑，强调在缺乏充分利润垫时主动减仓控制隔夜风险。</p></body></html>`;
for (const [name, bt, opts] of [["webkit", webkit, {}], ["chromium", chromium, { executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" }]]) {
  const b = await bt.launch(opts);
  const p = await b.newPage({ viewport: { width: 390, height: 400 } });
  const res = {};
  for (const [k, css] of [["justify", "text-align:justify"], ["justify+pretty", "text-align:justify;text-wrap:pretty"], ["pretty", "text-wrap:pretty"]]) {
    await p.setContent(html(css));
    res[k] = await p.evaluate(() => {
      const el = document.querySelector("p"); const r = document.createRange(); const out = []; let top = null, cur = "";
      const t = el.firstChild;
      for (let i = 0; i < t.data.length; i++) { r.setStart(t, i); r.setEnd(t, i + 1); const b = r.getClientRects()[0]; if (top !== null && b.top > top + 4) { out.push(cur.length); cur = ""; } top = b.top; cur += t.data[i]; }
      out.push(cur.length); return out.join(",");
    });
  }
  console.log(name, JSON.stringify(res));
  await b.close();
}
