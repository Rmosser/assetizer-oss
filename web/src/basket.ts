/* The 选片篮 of the browser extension (extension/): what it hands over, and what the page tells it back. The page never
   calls the extension; they talk through window.postMessage with the extension's content script on this page.
     page → ext   {proto, type: "want", batch?}        a batch id from #pick=…, or none: any batch not yet handed over
     ext  → page  {proto, type: "ready"}               the content script came up after the page: ask again
     ext  → page  {proto, type: "batch", batch, items} what to open the 新专题 sheet with
     page → ext   {proto, type: "done", batch, taken, dropped}  keys to take out of the basket (all else stays)
   See docs/topics.md「选片扩展」. */
import { useEffect, useState } from "react";
import { navigate } from "./router";

export type BasketItem = {
  key: string; site: string; kind: "video" | "list"; id: string; url: string;
  title: string | null; channel: string | null; dur: number | null; thumb: string | null;
};
export type Incoming = { batch: string; items: BasketItem[] };

const PROTO = "assetizer-basket";
const BATCH = /^[A-Za-z0-9_-]{8,64}$/;
const VIDEO = /^[A-Za-z0-9_-]{11}$/;
const LIST = /^[A-Za-z0-9_-]{10,64}$/;
const MAX = 200;

let wanted: string | undefined;
let pending: Incoming | null = null;
const subs = new Set<(x: Incoming | null) => void>();

const send = (m: object) => window.postMessage({ proto: PROTO, ...m }, location.origin);
const str = (v: unknown, n: number) => (typeof v === "string" && v.trim() ? v.trim().slice(0, n) : null);

/** Only what the 新专题 sheet can use: YouTube videos and playlists, with their links made here from their ids */
function clean(raw: unknown): BasketItem[] {
  if (!Array.isArray(raw)) return [];
  const out: BasketItem[] = [];
  const seen = new Set<string>();
  for (const x of raw.slice(0, MAX) as any[]) {
    if (!x || x.site !== "youtube" || typeof x.id !== "string") continue;
    const video = x.kind === "video" && VIDEO.test(x.id), list = x.kind === "list" && LIST.test(x.id);
    if (!video && !list) continue;
    const key = video ? `yt:${x.id}` : `yt-list:${x.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      key, site: "youtube", kind: video ? "video" : "list", id: x.id,
      url: video ? `https://www.youtube.com/watch?v=${x.id}` : `https://www.youtube.com/playlist?list=${x.id}`,
      title: str(x.title, 500), channel: str(x.channel, 200),
      dur: typeof x.dur === "number" && x.dur > 0 && x.dur <= 86400 ? Math.round(x.dur) : null,
      thumb: video ? `https://i.ytimg.com/vi/${x.id}/mqdefault.jpg` : null,
    });
  }
  return out;
}

function ask() {
  const m = location.hash.match(/^#pick=([A-Za-z0-9_-]{8,64})$/);
  if (m) {
    wanted = m[1];
    // out of the address: going back to it must not open the same batch again
    history.replaceState(history.state, "", location.pathname + location.search);
  }
  send({ type: "want", batch: wanted });
}

/** Once, as the app starts */
export function startBasket() {
  addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== location.origin) return;
    const m = e.data;
    if (!m || m.proto !== PROTO) return;
    if (m.type === "ready") ask();
    else if (m.type === "batch" && typeof m.batch === "string" && BATCH.test(m.batch)) {
      const items = clean(m.items);
      if (!items.length) return;
      wanted = undefined;
      pending = { batch: m.batch, items };
      if (location.pathname !== "/topics") navigate("/topics");
      subs.forEach((f) => f(pending));
    }
  });
  ask();
}

/** The batch waiting to be opened, and taking it (the sheet has it now) */
export function useIncoming(): [Incoming | null, () => void] {
  const [x, setX] = useState<Incoming | null>(pending);
  useEffect(() => { subs.add(setX); setX(pending); return () => { subs.delete(setX); }; }, []);
  return [x, () => { pending = null; subs.forEach((f) => f(null)); }];
}

/** Handed over: these leave the basket (in the topic now, or looked at and left out); the rest stays */
export function handed(batch: string, taken: string[], dropped: string[]) {
  send({ type: "done", batch, taken, dropped });
}
