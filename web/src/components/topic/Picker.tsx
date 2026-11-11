/* 新专题 and 加视频: a question (new topics only), then the videos — links, playlists, channel pages or words to search,
   one a line. Each line is read as it comes (/api/topics/peek, four at a time) into a list to tick: the videos of a link
   or a list are ticked, a search's are not. The foot says what the batch takes of the Gemini week.
   `incoming`: a 选片篮 from the browser extension (basket.ts) — its videos one group, ticked and shown at once from what
   the extension read, checked in the background like any line; its playlists as lines. With it the title becomes
   新专题 ｜ 已有专题, and the extension is told what left the basket once it is in. */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { gdrive, get, HttpError, type Pass, post } from "../../api";
import { Words } from "../../cjk";
import { type Incoming, handed } from "../../basket";
import { navigate } from "../../router";
import { PassSeg } from "../GDriveRules";
import { Sheet, type SheetHandle } from "../Sheet";
import { hours, len, share, Thumb } from "./bits";

export type Budget = { weekly_left: number | null; floor: number; per_hour: number; per_video: number; gemini?: boolean };
type Peeked = {
  id: string; url: string; title: string | null; channel: string | null; duration: number | null; published: string | null;
  thumb: string; availability?: string | null; live?: boolean; drafted?: boolean;
};
type Found = {
  text: string; state: "loading" | "ok" | "err"; kind?: "video" | "list" | "search"; title?: string | null; videos?: Peeked[]; error?: string;
  total?: number | null; more?: boolean; busy?: boolean;   // a list longer than one read: how long, and is there more to list
};
type PeekAnswer = { kind: Found["kind"]; title?: string; videos: Peeked[]; total?: number | null; more?: boolean };

const MOST = 100;          // a topic takes at most this many at once (web/topics.py NewTopic)
const MORE = 50;           // a list's videos listed at a time (web/topics.py PEEK_MOST)
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

function Row({ v, on, note, off, onToggle }: { v: Peeked; on: boolean; note?: string; off?: boolean; onToggle: () => void }) {
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

function Group({ f, picked, have, toggle, setMany, drop, label, unread, shut, onMore }: {
  f: Found; picked: Set<string>; have?: Set<string>; toggle: (id: string) => void; setMany: (ids: string[], on: boolean) => void; drop: () => void;
  label?: string; unread?: Set<string>; shut?: Map<string, string>; onMore?: () => void;
}) {
  const [all, setAll] = useState(false);
  const noteOf = (v: Peeked) => (have?.has(v.id) ? "已在专题里" : v.live ? "直播未结束" : shut?.get(v.id) || NOTE[v.availability || ""]
    || (unread?.has(v.id) ? "没核对上" : undefined));
  const can = (v: Peeked) => !have?.has(v.id) && !v.live && v.availability !== "private" && !shut?.has(v.id);
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
  if (f.kind === "video" && vs[0]) return <Row v={vs[0]} on={picked.has(vs[0].id)} note={noteOf(vs[0])} off={!can(vs[0])} onToggle={() => toggle(vs[0].id)} />;
  const ok = vs.filter(can);
  const n = ok.filter((v) => picked.has(v.id)).length;
  const full = n === ok.length && ok.length > 0;
  return (
    <section className="tp-fd">
      <header className="tp-fd-h">
        <span className="tp-fd-l">{label || (f.kind === "search" ? <>搜索「{f.text}」</> : f.title || short(f.text))}</span>
        <span className="tp-fd-n num">{n ? `${n}/` : ""}{vs.length} 期{f.more ? (f.total ? `，共 ${f.total} 期` : "，后面还有") : ""}</span>
        {ok.length > 0 && <button type="button" className="link" onClick={() => setMany(ok.map((v) => v.id), !full)}>{full ? "全不选" : "全选"}</button>}
      </header>
      {(all ? vs : vs.slice(0, SHOWN)).map((v) => <Row key={v.id} v={v} on={picked.has(v.id)} note={noteOf(v)} off={!can(v)} onToggle={() => toggle(v.id)} />)}
      {!all && vs.length > SHOWN && <button type="button" className="tp-fd-more" onClick={() => setAll(true)}>还有 {vs.length - SHOWN} 期</button>}
      {(all || vs.length <= SHOWN) && f.more && onMore && (
        <button type="button" className="tp-fd-more" onClick={onMore} disabled={f.busy}>{f.busy ? "正在列…" : `再列 ${MORE} 期`}</button>
      )}
    </section>
  );
}

/** `tid`: add to that topic (`have`: its videos), else a new topic. `onAdded`: the topic as it is after adding. */
export function Picker({ budget, onClose, tid: into, have: had, onAdded, incoming, topics }: {
  budget: Budget; onClose: () => void; tid?: number; have?: Set<string>; onAdded?: (view: any) => void;
  incoming?: Incoming; topics?: { id: number; question: string }[];
}) {
  // 新专题 ｜ 已有专题: only for a basket handed over, and only when there is a topic to add to
  const [old, setOld] = useState<{ id: number; question: string } | null>(null);
  const [choosing, setChoosing] = useState(false);
  const [oldHave, setOldHave] = useState<Set<string> | undefined>();
  useEffect(() => {
    if (!old) { setOldHave(undefined); return; }
    let on = true;
    get<{ items: { id: string }[] }>(`/api/topics/${old.id}`).then((v) => on && setOldHave(new Set(v.items.map((i) => i.id)))).catch(() => {});
    return () => { on = false; };
  }, [old]);
  const tid = into ?? old?.id;
  const have = into != null ? had : oldHave;
  const fresh = into == null;   // the sheet of 新专题 (it may still add to a topic that is there: 已有专题)
  const adding = tid != null;
  const kept = useMemo(() => {
    if (!fresh) return null;
    try { return JSON.parse(sessionStorage.getItem(KEEP) || "null"); } catch { return null; }
  }, [fresh]);
  const [q, setQ] = useState<string>(kept?.q || "");
  const [notes, setNotes] = useState<string>(kept?.notes || "");
  const [raw, setRaw] = useState<string>(kept?.raw || "");
  const [typing, setTyping] = useState(false);
  const [settled, setSettled] = useState(raw);
  // the basket: its videos are a group of their own, read in the background (silent lines); its playlists are lines
  const batch = useMemo<Peeked[]>(() => (incoming?.items || []).filter((i) => i.kind === "video").map((i) => ({
    id: i.id, url: i.url, title: i.title, channel: i.channel, duration: i.dur, published: null, thumb: i.thumb || "",
  })), [incoming]);
  const silent = useMemo(() => new Set(batch.map((v) => v.url)), [batch]);
  const [lists, setLists] = useState<string[]>(() => (incoming?.items || []).filter((i) => i.kind === "list").map((i) => i.url));
  const [picked, setPicked] = useState<Set<string>>(() => new Set([...(kept?.picked || []), ...batch.map((v) => v.id)]));
  // 网盘备份 of a new topic: chosen before it starts, when there is a drive to back up to (undefined: still asking)
  const [drive, setDrive] = useState<boolean | undefined>(fresh ? undefined : false);
  const [backup, setBackup] = useState<Pass>(kept?.backup ?? null);
  useEffect(() => {
    if (!fresh) return;
    let on = true;
    gdrive.ready().then((r) => on && setDrive(!!r.ready), () => on && setDrive(false));
    return () => { on = false; };
  }, [fresh]);
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
  const known = useRef(new Map<string, Peeked>(batch.map((v) => [v.id, v])));
  const shut = useRef(new Map<string, string>());   // basket videos YouTube will not open for this account, and why
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
  const lines = useMemo(() => [...new Set([...lists, ...linesOf(pending ? settled.slice(0, settled.lastIndexOf("\n") + 1) : settled)])],
    [settled, pending, lists]);
  current.current = [...lines, ...silent];

  const pump = useCallback(() => {
    while (flying.current.size < AT_ONCE && queue.current.length) {
      const line = queue.current.shift()!;
      if (cache.current.has(line) || flying.current.has(line) || !current.current.includes(line)) continue;
      flying.current.add(line);
      get<PeekAnswer>(`/api/topics/peek?url=${encodeURIComponent(line)}`)
        .then((r) => {
          cache.current.set(line, { text: line, state: "ok", ...r });
          r.videos.forEach((v) => known.current.set(v.id, v));
          if (silent.has(line)) {  // a video of the basket: ticked already; one that cannot be read is not
            const v = r.videos[0];
            if (v && (v.live || v.availability === "private")) setPicked((p) => { const n = new Set(p); n.delete(v.id); return n; });
            return;
          }
          // a link or a list: what it holds is wanted (a line brought back from a half-made topic keeps its ticks)
          if (r.kind !== "search" && alive.current && current.current.includes(line) && !restored.current.delete(line)) {
            setPicked((p) => {
              const n = new Set(p);
              r.videos.forEach((v) => { if (!have?.has(v.id) && !v.live && v.availability !== "private") n.add(v.id); });
              return n;
            });
          }
        })
        .catch((e) => {
          cache.current.set(line, { text: line, state: "err", error: e instanceof TypeError ? "连不上课代表" : String(e.message || e) });
          // a basket video this account will not open (members only, private, gone, a sign-in): said, and not ticked
          const v = silent.has(line) ? batch.find((x) => x.url === line) : undefined;
          if (v && e instanceof HttpError && e.status === 422) {
            shut.current.set(v.id, e.message);
            setPicked((p) => { const n = new Set(p); n.delete(v.id); return n; });
          }
        })
        .finally(() => { flying.current.delete(line); if (alive.current) { bump((n) => n + 1); pump(); } });
    }
  }, [have, silent, batch]);
  useEffect(() => {
    // a line that could not be read is read again once it is taken out and put back (a link mended, YouTube back)
    for (const [l, f] of cache.current) if (f.state === "err" && !lines.includes(l)) cache.current.delete(l);
    for (const l of [...lines, ...silent]) if (!cache.current.has(l) && !flying.current.has(l) && !queue.current.includes(l)) queue.current.push(l);
    pump();
  }, [lines, silent, pump]);

  const found = lines.map((l) => cache.current.get(l) || { text: l, state: "loading" as const });
  const basket: Found | null = batch.length ? { text: "选片篮", state: "ok", kind: "list", videos: batch.map((v) => known.current.get(v.id) || v) } : null;
  const unread = new Set(batch.filter((v) => cache.current.get(v.url)?.state === "err" && !shut.current.has(v.id)).map((v) => v.id));
  // ticked and still on the list
  const shown = new Set([...batch.map((v) => v.id), ...found.flatMap((f) => (f.videos || []).map((v) => v.id))]);
  // (a basket's videos come ticked: those already in the topic chosen under 已有专题 are not counted)
  const chosen = [...picked].filter((id) => shown.has(id) && known.current.has(id) && !have?.has(id)).map((id) => known.current.get(id)!);
  const secs = chosen.reduce((s, v) => s + (v.duration || 0), 0);
  const cost = chosen.reduce((s, v) => s + (v.drafted ? 0 : budget.per_hour * (v.duration || 0) / 3600) + budget.per_video, 0);
  const left = budget.weekly_left;
  const room = left == null ? null : Math.max(0, left - budget.floor);
  const over = room != null && cost > room;
  const reading = found.some((f) => f.state === "loading");

  useEffect(() => {  // a new topic half made outlives the sheet closing by mistake, until it is started
    if (!fresh) return;
    try { sessionStorage.setItem(KEEP, JSON.stringify({ q, notes, raw, picked: [...picked], backup })); } catch { /* private mode */ }
  }, [fresh, q, notes, raw, picked, backup]);

  const toggle = (id: string) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  /** The next videos of a list longer than one read, ticked like the first */
  const more = async (line: string) => {
    const f = cache.current.get(line);
    if (!f || f.state !== "ok" || f.busy) return;
    cache.current.set(line, { ...f, busy: true });
    bump((n) => n + 1);
    try {
      const r = await get<PeekAnswer>(`/api/topics/peek?url=${encodeURIComponent(line)}&start=${(f.videos?.length || 0) + 1}`);
      const seen = new Set((f.videos || []).map((v) => v.id));
      const add = r.videos.filter((v) => !seen.has(v.id));
      add.forEach((v) => known.current.set(v.id, v));
      cache.current.set(line, { ...f, busy: false, videos: [...(f.videos || []), ...add], more: r.more, total: r.total ?? f.total });
      if (f.kind !== "search") setPicked((p) => {
        const n = new Set(p);
        add.forEach((v) => { if (!have?.has(v.id) && !v.live && v.availability !== "private") n.add(v.id); });
        return n;
      });
    } catch (e) {
      cache.current.set(line, { ...f, busy: false });
      setErr((e as Error).message);
    }
    if (alive.current) bump((n) => n + 1);
  };
  const setMany = (ids: string[], on: boolean) => setPicked((p) => { const n = new Set(p); ids.forEach((id) => (on ? n.add(id) : n.delete(id))); return n; });
  // what linesOf read goes, and only that (taking out 「AI」 leaves 「AI 芯片」): a line of words, or a link — the line
  // with it when it was the line's only link (the words around a link were never read)
  const drop = (entry: string) => { setLists((l) => l.filter((x) => x !== entry)); setRaw((r) => r.split("\n").flatMap((l) => {
    const t = l.trim(), urls = t.match(LINKS);
    if (!urls) return t.slice(0, 200) === entry ? [] : [l];
    if (!urls.includes(entry)) return [l];
    return urls.every((u) => u === entry) ? [] : [t.replace(LINKS, (u) => (u === entry ? "" : u)).replace(/\s{2,}/g, " ").trim()];
  }).filter((l, i, a) => l.trim() || i === a.length - 1).join("\n")); };

  /** What leaves the basket: what went into the topic (or was in it already), and what was looked at and left out —
   *  unless the basket held more than a topic takes, when what was left out may be for the next one */
  const tell = (sent: Set<string>) => {
    if (!incoming) return;
    const taken: string[] = [], dropped: string[] = [];
    const leave = incoming.items.length <= MOST;
    for (const it of incoming.items) {
      if (it.kind === "video") {
        if (sent.has(it.id) || have?.has(it.id)) taken.push(it.key);
        else if (leave && !picked.has(it.id)) dropped.push(it.key);
      } else {
        const f = cache.current.get(it.url);
        if ((f?.videos || []).some((v) => sent.has(v.id))) taken.push(it.key);
        else if (leave && f?.state === "ok") dropped.push(it.key);
      }
    }
    handed(incoming.batch, taken, dropped);
  };

  const asks = !adding && !choosing && !!drive;  // 加视频 (and 已有专题) go by the topic's own
  const ready = chosen.length > 0 && chosen.length <= MOST && (adding || q.trim().length >= 2) && !busy && !choosing
    && (adding || (drive !== undefined && (!asks || backup != null)));
  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!adding && q.trim().length < 2) { qRef.current?.focus(); return; }
    if (!ready) return;
    setBusy(true);
    setErr(null);
    const videos = chosen.map((v) => ({ id: v.id, title: v.title, channel: v.channel, duration: v.duration == null ? null : Math.min(v.duration, 86400), published: v.published }));
    const sent = new Set(videos.map((v) => v.id));
    try {
      if (!adding) {
        const r = await post<{ id: number }>("/api/topics", {
          question: q.trim(), notes: notes.trim() || null, videos, batch: incoming?.batch, ...(asks && backup != null ? { backup } : {}) });
        try { sessionStorage.removeItem(KEEP); } catch { /* private mode */ }
        tell(sent);
        sheet.current?.close(() => { onClose(); navigate(`/topics/${r.id}`); });
      } else {
        const view = await post(`/api/topics/${tid}/videos`, { videos });
        tell(sent);
        if (into != null) { onAdded?.(view); sheet.current?.close(); }
        else sheet.current?.close(() => { onClose(); navigate(`/topics/${tid}`); });
      }
    } catch (x) {
      setErr((x as Error).message);
      setBusy(false);
    }
  };
  const onKey = (e: React.KeyboardEvent) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void submit(); } };

  const both = !!incoming && !!topics?.length;
  const title = both ? (
    <span className="seg tp-mode">
      <button type="button" className={old || choosing ? "" : "on"} aria-pressed={!(old || choosing)} onClick={() => { setOld(null); setChoosing(false); }}>新专题</button>
      <button type="button" className={old || choosing ? "on" : ""} aria-pressed={!!(old || choosing)} onClick={() => { if (!old) setChoosing(true); }}>已有专题</button>
    </span>
  ) : fresh ? "新专题" : "加视频";

  const sum = chosen.length ? [
    `${chosen.length} 期`, hours(secs / 3600),
    budget.gemini !== false && `约用 Gemini 周额度 ${share(cost)}${room != null ? (over ? `，可用的只剩 ${share(room)}` : `，还剩 ${share(left!)}`) : ""}`,
  ].filter(Boolean).join(" · ") : "";

  return (
    <Sheet ref={sheet} title={title} onClose={onClose} tall className="tp-sheet">
      <form className="tp-pick" onSubmit={submit} onKeyDown={onKey}>
        {choosing && (
          <div className="tp-olds">
            {topics!.map((t) => (
              <button type="button" key={t.id} className={`tp-old${old?.id === t.id ? " on" : ""}`} onClick={() => { setOld(t); setChoosing(false); }}>
                <Words>{t.question}</Words>
              </button>
            ))}
          </div>
        )}
        {old && !choosing && (
          <button type="button" className="tp-q tp-q-old" onClick={() => setChoosing(true)} aria-label="换一个专题"><Words>{old.question}</Words></button>
        )}
        {!adding && !choosing && (
          <>
            <textarea ref={qRef} className="tp-q" value={q} rows={1} maxLength={500} onChange={(e) => setQ(e.target.value)}
              placeholder="想弄清什么" aria-label="问题" data-autofocus={!q || undefined} />
            <textarea ref={nRef} className="tp-notes" value={notes} rows={1} maxLength={4000} onChange={(e) => setNotes(e.target.value)}
              placeholder="要什么、不要什么（可不填）" aria-label="补充" />
          </>
        )}
        <textarea ref={uRef} className="tp-urls" value={raw} rows={2} onChange={(e) => setRaw(e.target.value)} spellCheck={false}
          onFocus={() => setTyping(true)} onBlur={() => { setTyping(false); setSettled(raw); }} data-autofocus={!fresh || !!q || undefined}
          placeholder={basket ? "还要加的视频、列表或关键词" : "视频、播放列表或频道链接，一行一个；也可以写关键词搜索"} aria-label="视频" />
        {pending.length >= 2 && <p className="tp-hint">回车搜索「{pending.slice(0, 40)}」</p>}
        <div className="tp-found">
          {basket && <Group f={basket} label="选片篮" picked={picked} have={have} unread={unread} shut={shut.current} toggle={toggle} setMany={setMany} drop={() => {}} />}
          {found.map((f) => <Group key={f.text} f={f} picked={picked} have={have} toggle={toggle} setMany={setMany} drop={() => drop(f.text)} onMore={() => void more(f.text)} />)}
        </div>
        <footer className={`tp-pick-foot${asks ? " bk" : ""}`}>
          <p className={`tp-sum${over ? " bad" : ""}`} aria-live="polite">
            {err ? <span className="zhu">{err}</span> : chosen.length > MOST ? <span className="zhu">一次最多 {MOST} 期</span> : sum || (reading ? "正在读取…" : "")}
          </p>
          {asks && <span className="tp-bk"><span>网盘备份</span><PassSeg value={backup} label="网盘备份" onChange={setBackup} /></span>}
          <button type="submit" className="btn solid" disabled={!ready}>{busy ? (adding ? "正在加…" : "正在建…") : adding ? "加进来" : "开始"}</button>
        </footer>
      </form>
    </Sheet>
  );
}
