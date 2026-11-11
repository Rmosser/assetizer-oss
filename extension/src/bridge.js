// 课代表页面上的内容脚本：页面来要这一批时给它，页面说「交好了」时从篮子里移出它说的那些。
// 协议见课代表的 web/src/basket.ts。只在设置里选的那台盒子上应答（同时开着开发盒子时不串）。
(() => {
  const AZ = globalThis.AZ;
  const PROTO = "assetizer-basket";
  const post = (m) => window.postMessage({ proto: PROTO, ...m }, location.origin);
  const mine = async () => (await AZ.boxes.current()).origin === location.origin;
  const alive = () => !!chrome.runtime?.id;

  async function give(batch) {
    if (!alive() || !(await mine())) return;
    const out = await AZ.handoff.claim(batch);
    if (out) post({ type: "batch", batch: out.batch, items: out.items });
  }

  window.addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== location.origin) return;
    const m = e.data;
    if (!m || m.proto !== PROTO || !alive()) return;
    if (m.type === "want") give(typeof m.batch === "string" ? m.batch : undefined);
    else if (m.type === "done" && typeof m.batch === "string") {
      mine().then((ok) => ok && AZ.handoff.done(m.batch, m.taken, m.dropped));
    }
  });

  // 扩展的后台切到这个已开着的标签页时
  chrome.runtime.onMessage.addListener((msg) => { if (msg?.type === "offer") give(msg.batch); });

  // 页面可能比这段脚本先起来、已经问过了：告诉它再问一次
  post({ type: "ready" });
})();
