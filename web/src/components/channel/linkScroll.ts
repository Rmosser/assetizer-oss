import { useEffect } from "react";

/** The two Notion pages side by side scroll as one: units of the same stretch of video (compare's pairs) reach the top of
 *  their panes together, and in between the two pages keep pace. On every change of `ready` the left page lines up with
 *  the right one. Positions are read on each scroll: images load late and move what is below them. */
export function useLinkedScroll(
  left: React.RefObject<HTMLElement | null>, right: React.RefObject<HTMLElement | null>,
  pairs: { a: string; b: string }[], on: boolean, ready: string,
) {
  useEffect(() => {
    const A = left.current, B = right.current;
    if (!on || !A || !B) return;
    const marks = pairs.flatMap((p) => {
      const ea = A.querySelector(`[data-k~="${CSS.escape(`a:${p.a}`)}"]`), eb = B.querySelector(`[data-k~="${CSS.escape(`u:${p.b}`)}"]`);
      return ea && eb ? [[ea, eb] as const] : [];
    });
    const y = (pane: HTMLElement, el: Element) => el.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop;
    /** [left, right] scroll positions that belong together, rising on both sides */
    const table = () => {
      const maxA = A.scrollHeight - A.clientHeight, maxB = B.scrollHeight - B.clientHeight;
      const t: [number, number][] = [[0, 0]];
      for (const [ea, eb] of marks) {
        const ya = y(A, ea), yb = y(B, eb), [pa, pb] = t[t.length - 1];
        if (ya > pa && yb > pb && ya < maxA && yb < maxB) t.push([ya, yb]);
      }
      if (maxA > t[t.length - 1][0] && maxB > t[t.length - 1][1]) t.push([maxA, maxB]);
      return t;
    };
    const follow = (from: HTMLElement, to: HTMLElement, i: 0 | 1) => {
      const t = table(), at = from.scrollTop;
      let k = 0;
      while (k < t.length - 2 && t[k + 1][i] <= at) k++;
      const lo = t[k], hi = t[k + 1] || lo, span = hi[i] - lo[i];
      const v = span > 0 ? lo[1 - i] + ((at - lo[i]) / span) * (hi[1 - i] - lo[1 - i]) : lo[1 - i];
      if (Math.abs(to.scrollTop - v) >= 1) { moved.set(to, v); to.scrollTop = v; }
    };
    const moved = new WeakMap<HTMLElement, number>();  // where we just put a pane: its scroll event is not the reader's
    let lead: 0 | 1 = 1;  // the pane being read; a page changing height (a late image, an opened toggle) moves only the other
    const on_ = (from: HTMLElement, to: HTMLElement, i: 0 | 1) => () => {
      const m = moved.get(from);
      moved.delete(from);
      if (m === undefined || Math.abs(from.scrollTop - m) >= 2) { lead = i; follow(from, to, i); }
    };
    const fa = on_(A, B, 0), fb = on_(B, A, 1);
    A.addEventListener("scroll", fa, { passive: true });
    B.addEventListener("scroll", fb, { passive: true });
    const ro = new ResizeObserver(() => { if (lead) follow(B, A, 1); else follow(A, B, 0); });
    [A, B].forEach((pane) => { const page = pane.querySelector(".nb"); if (page) ro.observe(page); });
    follow(B, A, 1);
    return () => { A.removeEventListener("scroll", fa); B.removeEventListener("scroll", fb); ro.disconnect(); };
  }, [left, right, on, ready]); // eslint-disable-line react-hooks/exhaustive-deps
}

/** Keep what is being read in place while a pane changes width and its text reflows: call before, then call the result after. */
export function keepPlace(pane: HTMLElement | null) {
  if (!pane) return () => {};
  const top = pane.getBoundingClientRect().top;
  const el = [...pane.querySelectorAll(".blk")].find((b) => b.getBoundingClientRect().top >= top);
  if (!el) return () => {};
  const off = el.getBoundingClientRect().top - top;
  return () => { pane.scrollTop += el.getBoundingClientRect().top - pane.getBoundingClientRect().top - off; };
}
