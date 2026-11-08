/* 新专题 and 加视频: a question (new topics only), then the videos — links, playlists, channel pages or words to search,
   one a line. Each line is read as it comes (/api/topics/peek, four at a time) into a list to tick: the videos of a link
   or a list are ticked, a search's are not. The foot says what the batch takes of the Gemini week. */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { get, post } from "../../api";
import { Words } from "../../cjk";
import { navigate } from "../../router";
import { Sheet, type SheetHandle } from "../Sheet";
import { hours, len, share, Thumb } from "./bits";

export type Budget = { weekly_left: number | null; floor: number; per_hour: number; per_video: number; gemini?: boolean };
type Peeked = {
  id: string; url: string; title: string | null; channel: string | null; duration: number | null; published: string | null;
  thumb: string; availability?: string | null; live?: boolean; drafted?: boolean;
};
type Found = { text: string; state: "loading" | "ok" | "err"; kind?: "video" | "list" | "search"; title?: string | null; videos?: Peeked[]; error?: string };

const MOST = 100;          // a topic takes at most this many at once (web/topics.py NewTopic)
const SHOWN = 8;           // a list's rows before 还有 N 期
const AT_ONCE = 4;
const KEEP = "az-topic-new";
const LINKS = /https?:\/\/\S+/g;
const LINK = /https?:\/\//;
const NOTE: Record<string, string> = { private: "私享", premium_only: "Premium", subscriber_only: "会员", needs_auth: "要登录" };

/** The lines to read: every link on its own, else the line as words to search */
function linesOf(raw: string): string[] {
  const out: string[] = [];
  for (const l of raw.split("\n")) {
    const t = l.trim();
    if (!t) continue;
    const urls = t.match(LINKS);
    out.push(...(urls || [t.slice(0, 200)]));
  }
  return [...new Set(out)];
}

const short = (s: string) => s.replace(/^https?:\/\/(www\.|m\.)?/, "").slice(0, 80);

/** A textarea as tall as what is in it. Measured again when its width changes: the sheet opens after its content is
 *  laid out (a closed dialog measures 0), and a narrower box wraps to more lines. */
function useGrow(ref: React.RefObject<HTMLTextAreaElement | null>, value: string) {
  const fit = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }, [ref]);
  useLayoutEffect(fit, [fit, value]);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let w = -1;
    const ro = new ResizeObserver(([e]) => { if (e.contentRect.width !== w) { w = e.contentRect.width; fit(); } });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, fit]);
}

function Row({ v, on, note, onToggle }: { v: Peeked; on: boolean; note?: string; onToggle: () => void }) {
  const off = !!note && (v.live || v.availability === "private" || note === "已在专题里");
  const meta = [v.channel, len(v.duration), v.drafted ? "有底稿" : null, note].filter(Boolean).join(" · ");
  return (
    <label className={`tp-pk${off ? " off" : ""}${on ? " on" : ""}`}>
      <input type="checkbox" checked={on} disabled={off} onChange={onToggle} />
      <Thumb src={v.thumb} />
      <span className="tp-pk-t">
        <b><Words>{v.title || v.id}</Words></b>
        {meta && <small>{meta}</small>}
      </span>
    </label>
  );
}

function Group({ f, picked, have, toggle, setMany, drop }: {
  f: Found; picked: Set<string>; have?: Set<string>; toggle: (id: string) => void; setMany: (ids: string[], on: boolean) => void; drop: () => void;
}) {
  const [all, setAll] = useState(false);
  const noteOf = (v: Peeked) => (have?.has(v.id) ? "已在专题里" : v.live ? "直播未结束" : NOTE[v.availability || ""]);
  const can = (v: Peeked) => !have?.has(v.id) && !v.live && v.availability !== "private";
  if (f.state === "loading") {
    return <div className="tp-fd wait" role="status"><span className="tp-fd-l">{short(f.text)}</span><i className="tp-dots" aria-label="正在读取" /></div>;
  }
  if (f.state === "err") {
    return (
      <div className="tp-fd err">
        <span className="tp-fd-l">{short(f.text)}</span><span className="tp-fd-e">{f.error}</span>
        <button type="button" className="tp-x" onClick={drop} aria-label="删掉这一行">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden><path d="M6 6l12 12M18 6 6 18" /></svg>
        </button>
      </div>
    );
  }
  const vs = f.videos || [];
  if (f.kind === "video" && vs[0]) return <Row v={vs[0]} on={picked.has(vs[0].id)} note={noteOf(vs[0])} onToggle={() => toggle(vs[0].id)} />;
  const ok = vs.filter(can);
  const n = ok.filter((v) => picked.has(v.id)).length;
  const full = n === ok.length && ok.length > 0;
  return (
    <section className="tp-fd">
      <header className="tp-fd-h">
        <span className="tp-fd-l">{f.kind === "search" ? <>搜索「{f.text}」</> : f.title || short(f.text)}</span>
        <span className="tp-fd-n num">{n ? `${n}/` : ""}{vs.length} 期</span>
        {ok.length > 0 && <button type="button" className="link" onClick={() => setMany(ok.map((v) => v.id), !full)}>{full ? "全不选" : "全选"}</button>}
      </header>
      {(all ? vs : vs.slice(0, SHOWN)).map((v) => <Row key={v.id} v={v} on={picked.has(v.id)} note={noteOf(v)} onToggle={() => toggle(v.id)} />)}
      {!all && vs.length > SHOWN && <button type="button" className="tp-fd-more" onClick={() => setAll(true)}>还有 {vs.length - SHOWN} 期</button>}
    </section>
  );
}

/** `tid`: add to that topic (`have`: its videos), else a new topic. `onAdded`: the topic as it is after adding. */
export function Picker({ budget, onClose, tid, have, onAdded }: {
  budget: Budget; onClose: () => void; tid?: number; have?: Set<string>; onAdded?: (view: any) => void;
}) {
  const fresh = tid == null;
  const kept = useMemo(() => {
    if (!fresh) return null;
    try { return JSON.parse(sessionStorage.getItem(KEEP) || "null"); } catch { return null; }
  }, [fresh]);
  const [q, setQ] = useState<string>(kept?.q || "");
  const [notes, setNotes] = useState<string>(kept?.notes || "");
  const [raw, setRaw] = useState<string>(kept?.raw || "");
  const [typing, setTyping] = useState(false);
  const [settled, setSettled] = useState(raw);
  const [picked, setPicked] = useState<Set<string>>(() => new Set(kept?.picked || []));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [, bump] = useState(0);
  const sheet = useRef<SheetHandle>(null);
  const qRef = useRef<HTMLTextAreaElement>(null), nRef = useRef<HTMLTextAreaElement>(null), uRef = useRef<HTMLTextAreaElement>(null);
  useGrow(qRef, q); useGrow(nRef, notes); useGrow(uRef, raw);

  // what was read: kept across edits of the box, so a line cut and pasted back is not read again
  const cache = useRef(new Map<string, Found>());
  const flying = useRef(new Set<string>());
  const queue = useRef<string[]>([]);
  const known = useRef(new Map<string, Peeked>());
  const alive = useRef(true);
  const current = useRef<string[]>([]);
  const restored = useRef(new Set<string>(kept?.raw ? linesOf(kept.raw) : []));
  useEffect(() => () => { alive.current = false; }, []);

  useEffect(() => { const t = setTimeout(() => setSettled(raw), 350); return () => clearTimeout(t); }, [raw]);
  // the line being typed is searched once it is finished: Enter, or leaving the box
  const pending = useMemo(() => {
    const last = settled.split("\n").pop()!.trim();
    return typing && last && !LINK.test(last) ? last : "";
  }, [settled, typing]);
  const lines = useMemo(() => linesOf(pending ? settled.slice(0, settled.lastIndexOf("\n") + 1) : settled), [settled, pending]);
  current.current = lines;

  const pump = useCallback(() => {
    while (flying.current.size < AT_ONCE && queue.current.length) {
      const line = queue.current.shift()!;
      if (cache.current.has(line) || flying.current.has(line) || !current.current.includes(line)) continue;
      flying.current.add(line);
      get<{ kind: Found["kind"]; title?: string; videos: Peeked[] }>(`/api/topics/peek?url=${encodeURIComponent(line)}`)
        .then((r) => {
          cache.current.set(line, { text: line, state: "ok", ...r });
          r.videos.forEach((v) => known.current.set(v.id, v));
          // a link or a list: what it holds is wanted (a line brought back from a half-made topic keeps its ticks)
          if (r.kind !== "search" && alive.current && current.current.includes(line) && !restored.current.delete(line)) {
            setPicked((p) => {
              const n = new Set(p);
              r.videos.forEach((v) => { if (!have?.has(v.id) && !v.live && v.availability !== "private") n.add(v.id); });
              return n;
            });
          }
        })
        .catch((e) => cache.current.set(line, { text: line, state: "err", error: e instanceof TypeError ? "连不上课代表" : String(e.message || e) }))
        .finally(() => { flying.current.delete(line); if (alive.current) { bump((n) => n + 1); pump(); } });
    }
  }, [have]);
  useEffect(() => {
    // a line that could not be read is read again once it is taken out and put back (a link mended, YouTube back)
    for (const [l, f] of cache.current) if (f.state === "err" && !lines.includes(l)) cache.current.delete(l);
    for (const l of lines) if (!cache.current.has(l) && !flying.current.has(l) && !queue.current.includes(l)) queue.current.push(l);
    pump();
  }, [lines, pump]);

  const found = lines.map((l) => cache.current.get(l) || { text: l, state: "loading" as const });
  // ticked and still on the list
  const shown = new Set(found.flatMap((f) => (f.videos || []).map((v) => v.id)));
  const chosen = [...picked].filter((id) => shown.has(id) && known.current.has(id)).map((id) => known.current.get(id)!);
  const secs = chosen.reduce((s, v) => s + (v.duration || 0), 0);
  const cost = chosen.reduce((s, v) => s + (v.drafted ? 0 : budget.per_hour * (v.duration || 0) / 3600) + budget.per_video, 0);
  const left = budget.weekly_left;
  const room = left == null ? null : Math.max(0, left - budget.floor);
  const over = room != null && cost > room;
  const reading = found.some((f) => f.state === "loading");

  useEffect(() => {  // a new topic half made outlives the sheet closing by mistake, until it is started
    if (!fresh) return;
    try { sessionStorage.setItem(KEEP, JSON.stringify({ q, notes, raw, picked: [...picked] })); } catch { /* private mode */ }
  }, [fresh, q, notes, raw, picked]);

  const toggle = (id: string) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const setMany = (ids: string[], on: boolean) => setPicked((p) => { const n = new Set(p); ids.forEach((id) => (on ? n.add(id) : n.delete(id))); return n; });
  // what linesOf read goes, and only that (taking out 「AI」 leaves 「AI 芯片」): a line of words, or a link — the line
  // with it when it was the line's only link (the words around a link were never read)
  const drop = (entry: string) => setRaw((r) => r.split("\n").flatMap((l) => {
    const t = l.trim(), urls = t.match(LINKS);
    if (!urls) return t.slice(0, 200) === entry ? [] : [l];
    if (!urls.includes(entry)) return [l];
    return urls.every((u) => u === entry) ? [] : [t.replace(LINKS, (u) => (u === entry ? "" : u)).replace(/\s{2,}/g, " ").trim()];
  }).filter((l, i, a) => l.trim() || i === a.length - 1).join("\n"));

  const ready = chosen.length > 0 && chosen.length <= MOST && (!fresh || q.trim().length >= 2) && !busy;
  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (fresh && q.trim().length < 2) { qRef.current?.focus(); return; }
    if (!ready) return;
    setBusy(true);
    setErr(null);
    const videos = chosen.map((v) => ({ id: v.id, title: v.title, channel: v.channel, duration: v.duration == null ? null : Math.min(v.duration, 86400), published: v.published }));
    try {
      if (fresh) {
        const r = await post<{ id: number }>("/api/topics", { question: q.trim(), notes: notes.trim() || null, videos });
        try { sessionStorage.removeItem(KEEP); } catch { /* private mode */ }
        sheet.current?.close(() => { onClose(); navigate(`/topics/${r.id}`); });
      } else {
        const view = await post(`/api/topics/${tid}/videos`, { videos });
        onAdded?.(view);
        sheet.current?.close();
      }
    } catch (x) {
      setErr((x as Error).message);
      setBusy(false);
    }
  };
  const onKey = (e: React.KeyboardEvent) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void submit(); } };

  const sum = chosen.length ? [
    `${chosen.length} 期`, hours(secs / 3600),
    budget.gemini !== false && `约用 Gemini 周额度 ${share(cost)}${room != null ? (over ? `，可用的只剩 ${share(room)}` : `，还剩 ${share(left!)}`) : ""}`,
  ].filter(Boolean).join(" · ") : "";

  return (
    <Sheet ref={sheet} title={fresh ? "新专题" : "加视频"} onClose={onClose} tall className="tp-sheet">
      <form className="tp-pick" onSubmit={submit} onKeyDown={onKey}>
        {fresh && (
          <>
            <textarea ref={qRef} className="tp-q" value={q} rows={1} maxLength={500} onChange={(e) => setQ(e.target.value)}
              placeholder="想弄清什么" aria-label="问题" data-autofocus={!q || undefined} />
            <textarea ref={nRef} className="tp-notes" value={notes} rows={1} maxLength={4000} onChange={(e) => setNotes(e.target.value)}
              placeholder="要什么、不要什么（可不填）" aria-label="补充" />
          </>
        )}
        <textarea ref={uRef} className="tp-urls" value={raw} rows={2} onChange={(e) => setRaw(e.target.value)} spellCheck={false}
          onFocus={() => setTyping(true)} onBlur={() => { setTyping(false); setSettled(raw); }} data-autofocus={!fresh || !!q || undefined}
          placeholder="视频、播放列表或频道链接，一行一个；也可以写关键词搜索" aria-label="视频" />
        {pending.length >= 2 && <p className="tp-hint">回车搜索「{pending.slice(0, 40)}」</p>}
        <div className="tp-found">
          {found.map((f) => <Group key={f.text} f={f} picked={picked} have={have} toggle={toggle} setMany={setMany} drop={() => drop(f.text)} />)}
        </div>
        <footer className="tp-pick-foot">
          <p className={`tp-sum${over ? " bad" : ""}`} aria-live="polite">
            {err ? <span className="zhu">{err}</span> : chosen.length > MOST ? <span className="zhu">一次最多 {MOST} 期</span> : sum || (reading ? "正在读取…" : "")}
          </p>
          <button type="submit" className="btn solid" disabled={!ready}>{busy ? "正在建…" : fresh ? "开始" : "加进来"}</button>
        </footer>
      </form>
    </Sheet>
  );
}
