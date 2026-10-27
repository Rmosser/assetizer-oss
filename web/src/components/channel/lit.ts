import { useEffect, useSyncExternalStore } from "react";

/** Hover/focus highlighting across the three panes without re-rendering them.
 *
 * Every element that stands for a fact carries `data-k="<key> <related keys…>"`: the source field it shows (f:…),
 * the title variable (tok:…), the Notion property (p:…), the unit (u:…), the chapter (c:…), a template block (tpl).
 * Pointing at an element lights its first key; one generated CSS rule lights every element whose list holds it.
 * The page stays still — a 771-block Notion replica is not re-rendered on every mouse move. */

type State = { key: string | null; el: Element | null };
let state: State = { key: null, el: null };
const subs = new Set<() => void>();
let sheet: HTMLStyleElement | null = null;

export function light(key: string | null, el: Element | null = null) {
  if (key === state.key && el === state.el) return;
  state = { key, el };
  if (!sheet) {
    sheet = document.createElement("style");
    sheet.dataset.chp = "lit";
    document.head.appendChild(sheet);
  }
  const k = key ? CSS.escape(key) : "";
  sheet.textContent = key ? `.chp [data-k~="${k}"].lt-bg{background:var(--chp-lit)}`
    + `.chp [data-k~="${k}"].lt-ring{background:var(--chp-lit);box-shadow:0 0 0 1.5px var(--chp-litr)}`
    + `.chp [data-k~="${k}"].lt-seg{background:var(--ink)!important}` : "";
  subs.forEach((f) => f());
}

export function useLit(): State {
  return useSyncExternalStore((f) => { subs.add(f); return () => { subs.delete(f); }; }, () => state);
}

/** One set of listeners on the page root: whatever [data-k] the pointer or the focus is on, light it. */
export function useLitDelegation(root: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const pick = (e: Event) => {
      const t = (e.target as Element | null)?.closest?.("[data-k]");
      if (!t || !el.contains(t)) return light(null);
      light((t.getAttribute("data-k") || "").split(" ")[0] || null, t);
    };
    const leave = () => light(null);
    const blur = (e: FocusEvent) => { if (!el.contains(e.relatedTarget as Node | null)) light(null); };
    el.addEventListener("mouseover", pick);
    el.addEventListener("focusin", pick);
    el.addEventListener("mouseleave", leave);
    el.addEventListener("focusout", blur);
    return () => {
      el.removeEventListener("mouseover", pick);
      el.removeEventListener("focusin", pick);
      el.removeEventListener("mouseleave", leave);
      el.removeEventListener("focusout", blur);
      light(null);
    };
  }, [root]);
}

/** data-k for a source field and everything filled from it. */
export const keys = (...ks: (string | false | null | undefined)[]) => ks.filter(Boolean).join(" ");
