import { useCallback, useEffect, useRef, useState } from "react";
import { get } from "./api";

export type PollError = { message: string; auth: boolean; at: number };

export function usePoll<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<PollError | null>(null);
  const [loadedAt, setLoadedAt] = useState<number>(0);
  const alive = useRef(true);
  const has = useRef(false);
  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await get<T>(path);
      if (alive.current) { has.current = true; setData(d); setError(null); setLoadedAt(Date.now()); }
    } catch (e) {
      const err = e as Error;
      // a cross-origin redirect to the Access login surfaces as a TypeError too
      if (alive.current) setError({ message: err.message || String(e), auth: err.name === "AuthExpired" || err.name === "TypeError", at: Date.now() });
    }
  }, [path]);
  useEffect(() => {
    alive.current = true;
    load();
    // without data, keep retrying quietly so a dropped connection heals by itself
    const every = interval || 0;
    const t = setInterval(() => { if (!document.hidden && (every || !has.current)) load(); }, every || 15000);
    return () => { alive.current = false; clearInterval(t); };
  }, [load, interval]);
  return { data, error, loadedAt, reload: load, setData };
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
