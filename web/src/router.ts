import { useEffect, useLayoutEffect, useState } from "react";
import { flushSync } from "react-dom";

// pages restore their own scroll position once their data is on screen
if ("scrollRestoration" in history) history.scrollRestoration = "manual";
let pendingY: number | null = null;
addEventListener("popstate", (e) => { pendingY = (e.state && typeof e.state.y === "number") ? e.state.y : 0; });

export function navigate(to: string, opts: { replace?: boolean; quiet?: boolean } = {}) {
  if (to === location.pathname + location.search) return;
  try { history.replaceState({ ...(history.state || {}), y: scrollY }, ""); } catch { /* ignore */ }
  if (opts.replace) history.replaceState({ y: opts.quiet ? scrollY : 0 }, "", to);
  else history.pushState({ y: 0 }, "", to);
  pendingY = null;
  // quiet: moving inside one tool page (a tab, a list item) — no page turn, keep the scroll position
  if (opts.quiet) { flushSync(() => window.dispatchEvent(new Event("az:nav"))); return; }
  const swap = () => { flushSync(() => window.dispatchEvent(new Event("az:nav"))); window.scrollTo({ top: 0 }); };
  // 翻开: a page turn between issues and pages where the browser supports view transitions
  const doc = document as Document & { startViewTransition?: (cb: () => void) => unknown };
  if (doc.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) doc.startViewTransition(swap);
  else swap();
}

export function usePath() {
  const [p, setP] = useState(location.pathname);
  useEffect(() => {
    const on = () => setP(location.pathname);
    window.addEventListener("popstate", on);
    window.addEventListener("az:nav", on);
    return () => { window.removeEventListener("popstate", on); window.removeEventListener("az:nav", on); };
  }, []);
  return p;
}

/** Whether the page opening was gone back (or forward) to, and so goes back to where the reader was (useRestoreScroll);
 *  not one opened afresh or reloaded. Read it as the page first renders: it holds until the scroll is restored. */
export const returning = () => pendingY != null;

/** After back/forward, return to where the reader was — once the page has rendered its data. */
export function useRestoreScroll(ready: boolean) {
  useLayoutEffect(() => {
    if (!ready || pendingY == null) return;
    const y = pendingY;
    pendingY = null;
    requestAnimationFrame(() => window.scrollTo({ top: y }));
  }, [ready]);
}

export function linkProps(to: string) {
  return {
    href: to,
    onClick: (e: React.MouseEvent) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(to);
    },
  };
}
