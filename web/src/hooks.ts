import { useCallback, useEffect, useRef, useState } from "react";
import { get } from "./api";
import { reduceMotion } from "./util";

export type PollError = { message: string; auth: boolean; at: number };

export function usePoll<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<PollError | null>(null);
  const [loadedAt, setLoadedAt] = useState<number>(0);
  const alive = useRef(true);
  const has = useRef(false);
  // answers come back in any order: one older than what is shown, or asked before something was changed here (`reload`
  // after a save; the page opening on another path), would put back what was there before
  const sent = useRef(0), floor = useRef(0), shown = useRef(0);
  const load = useCallback(async (fresh = false) => {
    if (!path) return;
    const mine = ++sent.current;
    if (fresh) floor.current = mine;
    const late = () => !alive.current || mine < floor.current || mine < shown.current;
    try {
      const d = await get<T>(path);
      if (late()) return;
      shown.current = mine;
      has.current = true; setData(d); setError(null); setLoadedAt(Date.now());
    } catch (e) {
      const err = e as Error;
      // a cross-origin redirect to the Access login surfaces as a TypeError too
      if (!late()) setError({ message: err.message || String(e), auth: err.name === "AuthExpired" || err.name === "TypeError", at: Date.now() });
    }
  }, [path]);
  const reload = useCallback(() => load(true), [load]);
  useEffect(() => {
    alive.current = true;
    load(true);
    // without data, keep retrying quietly so a dropped connection heals by itself
    const every = interval || 0;
    const t = setInterval(() => { if (!document.hidden && (every || !has.current)) load(); }, every || 15000);
    return () => { alive.current = false; clearInterval(t); };
  }, [load, interval]);
  // what is polled is read again the moment the page is back (a Mac waking, a tab brought forward, the network back),
  // not a whole interval later
  useBack(interval ? load : null);
  return { data, error, loadedAt, reload, setData };
}

/** Call `fn` when the page comes back into view, gets the focus again, or the network returns (at most every 2 s). */
export function useBack(fn: (() => void) | null) {
  useEffect(() => {
    if (!fn) return;
    let last = 0;
    const on = () => {
      if (document.hidden || Date.now() - last < 2000) return;
      last = Date.now();
      fn();
    };
    document.addEventListener("visibilitychange", on);
    addEventListener("focus", on);
    addEventListener("online", on);
    return () => {
      document.removeEventListener("visibilitychange", on);
      removeEventListener("focus", on);
      removeEventListener("online", on);
    };
  }, [fn]);
}

/** Run once per browser, remembered in localStorage (guarded: storage can be unavailable). */
export function useOnce(key: string): [boolean, () => void] {
  const [seen, setSeen] = useState(() => { try { return localStorage.getItem(key) === "1"; } catch { return false; } });
  const mark = useCallback(() => { try { localStorage.setItem(key, "1"); } catch { /* private mode */ } setSeen(true); }, [key]);
  return [seen, mark];
}

export function useStored<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => { try { const s = localStorage.getItem(key); return s ? JSON.parse(s) : initial; } catch { return initial; } });
  const set = useCallback((nv: T) => { setV(nv); try { localStorage.setItem(key, JSON.stringify(nv)); } catch { /* ignore */ } }, [key]);
  return [v, set];
}

/** Something that comes and goes (a toast, a docked bar): keep its last value on screen while it plays
 *  its exit (`leaving`), then let it go. Under reduced motion it goes at once. */
export function useLeaving<T>(value: T | null | undefined | false, ms = 240): { shown: T | null; leaving: boolean } {
  const last = useRef<T | null>(value || null);
  const [, gone] = useState(0);
  if (value) last.current = value;
  useEffect(() => {
    if (value || last.current === null) return;
    const drop = () => { last.current = null; gone((n) => n + 1); };
    if (reduceMotion()) { drop(); return; }
    const t = setTimeout(drop, ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return { shown: value || last.current, leaving: !value && last.current !== null };
}

export function useMedia(q: string) {
  const [m, setM] = useState(() => matchMedia(q).matches);
  useEffect(() => {
    const mq = matchMedia(q);
    const on = () => setM(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [q]);
  return m;
}
