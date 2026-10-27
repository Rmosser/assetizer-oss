import { useEffect, useLayoutEffect, useState } from "react";
import { useLit } from "./lit";

type Line = { id: string; d: string; x1: number; y1: number; x2: number; y2: number };

/** Connector lines for the lit fact, drawn only where the data really flows: source field → setting → Notion value.
 *  Lines run through the gaps between the columns, never across a pane. */
export function Wires({ host }: { host: React.RefObject<HTMLElement | null> }) {
  const { key, el } = useLit();
  const [lines, setLines] = useState<Line[]>([]);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const root = host.current;
    if (!root) return;
    let raf = 0;
    const on = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => setTick((t) => t + 1)); };
    root.addEventListener("scroll", on, true);
    addEventListener("resize", on);
    addEventListener("scroll", on, { passive: true });
    return () => { cancelAnimationFrame(raf); root.removeEventListener("scroll", on, true); removeEventListener("resize", on); removeEventListener("scroll", on); };
  }, [host]);
  useLayoutEffect(() => {
    const root = host.current;
    if (!root || !key || !/^(p:|tok:|f:)/.test(key) || matchMedia("(max-width: 1099px)").matches) { setLines((l) => (l.length ? [] : l)); return; }
    const br = root.getBoundingClientRect();
    const cols = [...root.children].filter((c) => c.tagName.toLowerCase() !== "svg").map((c) => c.getBoundingClientRect()).filter((r) => r.width > 40);
    const colOf = (r: DOMRect) => cols.find((q) => r.left >= q.left - 2 && r.right <= q.right + 2) || null;
    const visible = (e: Element): DOMRect | null => {
      const r = e.getBoundingClientRect();
      if ((!r.width && !r.height) || r.bottom < 0 || r.top > innerHeight) return null;
      for (let p = e.parentElement; p && p !== root; p = p.parentElement) {
        const oy = getComputedStyle(p).overflowY;
        if (oy === "auto" || oy === "scroll") {
          const q = p.getBoundingClientRect();
          if (r.bottom < q.top + 6 || r.top > q.bottom - 6) return null;
        }
      }
      return r;
    };
    const by: Record<string, DOMRect[]> = { src: [], mid: [], dst: [] };
    root.querySelectorAll(`[data-k~="${CSS.escape(key)}"]`).forEach((e) => {
      const side = e.getAttribute("data-side");
      if (!side || !(side in by)) return;
      const r = visible(e);
      if (r) by[side].push(r);
    });
    const hs = el && root.contains(el) ? el.getAttribute("data-side") : null;
    const hr = hs && el ? visible(el) : null;
    const src = hs === "src" && hr ? hr : by.src[0];
    const mid = hs === "mid" && hr ? hr : by.mid[0];
    const route = (a: DOMRect, b: DOMRect): Omit<Line, "id"> | null => {
      const ca = colOf(a), cb = colOf(b);
      if (!ca || !cb || ca === cb) return null;
      const x1 = a.right - br.left, y1 = (a.top + a.bottom) / 2 - br.top, x2 = b.left - br.left, y2 = (b.top + b.bottom) / 2 - br.top;
      const ex = ca.right - br.left, sx = cb.left - br.left, gx = (ex + sx) / 2;
      return { d: `M${x1},${y1} L${ex},${y1} C${gx},${y1} ${gx},${y2} ${sx},${y2} L${x2},${y2}`, x1, y1, x2, y2 };
    };
    const out: Omit<Line, "id">[] = [];
    if (src && mid) { const r = route(src, mid); if (r) out.push(r); }
    const from = mid || src;
    if (from) by.dst.slice(0, 2).forEach((d) => { if (d.left > from.right) { const r = route(from, d); if (r) out.push(r); } });
    // keyed by the lit fact, so moving straight to the next fact draws its lines afresh instead of bending the old ones
    setLines(out.map((l, i) => ({ ...l, id: `${key}#${i}` })));
  }, [key, el, tick, host]);
  return (
    <svg className="ch-wires" aria-hidden>
      {lines.map((l) => (
        <g key={l.id}>
          <path className="w" d={l.d} />
          <circle className="d" cx={l.x1} cy={l.y1} r="2.4" />
          <circle className="d" cx={l.x2} cy={l.y2} r="2.4" />
        </g>
      ))}
    </svg>
  );
}
