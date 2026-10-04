import { useCallback, useEffect, useRef, useState } from "react";
import { get } from "./api";

export function usePoll<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await get<T>(path);
      if (alive.current) { setData(d); setError(null); }
    } catch (e) {
      if (alive.current) setError(String(e));
    }
  }, [path]);
  useEffect(() => {
    alive.current = true;
    load();
    if (!interval) return () => { alive.current = false; };
    const t = setInterval(() => { if (!document.hidden) load(); }, interval);
    return () => { alive.current = false; clearInterval(t); };
  }, [load, interval]);
  return { data, error, reload: load, setData };
}
