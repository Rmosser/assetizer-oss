/* One video of a topic: its notes for the question, every fact citing the moment it comes from; the looks taken again at
   the original (what each model answered from the frames it was shown); the draft they were written from. A citation
   answers beside the notes: the player at that moment, the frame, the look. On a phone the player stays on top while it
   plays and a frame or a look comes up as a sheet. */
import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { get, type ItemBackup, post } from "../api";
import { typeset, Words } from "../cjk";
import { Armed, FIT, handoff, type Held, itemState, type Item, LeaveOut, len, Phases, STEP, thumbOf, useToast } from "../components/topic/bits";
import { type Cite, citeOf, renderCited } from "../components/topic/cite";
import { Lightbox } from "../components/Lightbox";
import { Sheet } from "../components/Sheet";
import { Fault, Stale, Unroll } from "../components/States";
import { useMedia, usePoll } from "../hooks";
import { linkProps, navigate } from "../router";
import { errorLabel, failText, pubDate } from "../util";
import "../topic.css";

type Look = {
  id: string; mode: string; start: string | number; end: string | number | null; times_shown: number[] | null; question: string;
  why?: string | null; answer: string | null; evidence?: string | null; sure?: boolean | null; answer2?: string | null;
  evidence2?: string | null; sure2?: boolean | null; error?: string | null; second?: boolean | null; target?: string | null;
  round: 1 | 2; media: string[];
};
type VView = {
  topic: { id: number; question: string; rev: number; paused: boolean };
  video: { id: string; url: string; title: string | null; channel: string | null; duration: number | null; published: string | null; thumb: string;
    gone: string | null };
  item: Item; plan: { fit: string; reason: string } | null; notes: string | null; looks: Look[];
  playback: boolean; rev: number; draft: boolean; frames: boolean; shots: string[]; held: Held;
  prev: string | null; next: string | null;
  /** 网盘备份: `restored`, the original fetched back from the drive; when */
  backup: ItemBackup | null; restored_at: string | null;
};
type Ev = { kind: "say" | "frame"; t: number; at: string } | { kind: "look"; id: string };

const NARROW = "(max-width: 900px)";
const MODE: Record<string, string> = { frame: "单帧", frames: "多帧", zoom: "放大", clip: "片段" };
const two = (n: number) => String(n).padStart(2, "0");
/** 754.5 → 12:34.5 */
const at = (t: number) => {
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const ss = Number.isInteger(s) ? two(s) : `${two(Math.floor(s))}.${Math.round((s % 1) * 10)}`;
  return h ? `${h}:${two(m)}:${ss}` : `${two(m)}:${ss}`;
};
const asSecs = (v: string | number | null | undefined): number | null => {
  if (v == null || v === "") return null;
  if (typeof v === "number") return v;
  const n = v.split(":").reduce((s, p) => s * 60 + parseFloat(p), 0);
  return Number.isFinite(n) ? n : null;
};

/* ───────────────────────── the player ───────────────────────── */
type PlayerHandle = { seek: (t: number, play?: boolean) => void };

/** The 360p copy kept for topics when there is one, else the YouTube embed; neither loads until it is played or a citation
 *  asks for a moment. */
function Player({ vid, local, title, ref }: { vid: string; local: boolean; title: string; ref: React.Ref<PlayerHandle> }) {
  const [on, setOn] = useState<{ t: number; play: boolean } | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const yt = (func: string, args: unknown[] = []) =>
    frame.current?.contentWindow?.postMessage(JSON.stringify({ event: "command", func, args }), "*");
  useImperativeHandle(ref, () => ({
    seek(t, play = true) {
      if (!on) { if (play) setOn({ t, play }); return; }  // a frame looked at does not start the player
      if (local && video.current) {
        video.current.currentTime = t;
        if (play) video.current.play().catch(() => {}); else video.current.pause();
      } else {
        yt("seekTo", [t, true]);
        yt(play ? "playVideo" : "pauseVideo");
      }
    },
  }), [on, local]);
  if (!on) {
    return (
      <button className="tp-player idle" onClick={() => setOn({ t: 0, play: true })} aria-label={`播放：${title}`}>
        <img src={thumbOf(vid, "hq")} alt="" onError={(e) => { e.currentTarget.style.visibility = "hidden"; }} />
        <span className="disc"><svg width="20" height="20" viewBox="0 0 24 24" aria-hidden><path d="M8 5.5v13l11-6.5-11-6.5Z" fill="currentColor" /></svg></span>
      </button>
    );
  }
  if (local) {
    return (
      <div className="tp-player">
        <video ref={video} src={`/api/sources/${encodeURIComponent(vid)}/playback`} controls playsInline autoPlay={on.play} preload="auto"
          onLoadedMetadata={(e) => { e.currentTarget.currentTime = on.t; }} />
      </div>
    );
  }
  const src = `https://www.youtube-nocookie.com/embed/${vid}?autoplay=${on.play ? 1 : 0}&playsinline=1&rel=0&enablejsapi=1&start=${Math.floor(on.t)}&origin=${encodeURIComponent(location.origin)}`;
  return (
    <div className="tp-player">
      <iframe ref={frame} src={src} title={title} allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowFullScreen referrerPolicy="strict-origin-when-cross-origin" />
    </div>
  );
}

/* ───────────────────────── a look ───────────────────────── */
/** Each image a look showed, with the moment of the original it is (a zoom's crop first, then its frame) */
function shotsOf(x: Look) {
  const ts = x.times_shown || [];
  return x.media.filter((m) => !/\.mp4(\?|$)/.test(m)).map((src, i) => ({
    id: `${x.id}-${i}`, src, start: (x.mode === "zoom" ? ts[0] : ts[i]) ?? asSecs(x.start) ?? 0,
    meta: { reason: x.mode === "zoom" && i === 0 && x.media.length > 1 ? `放大：${x.target || x.question}` : undefined },
  }));
}
const clipOf = (x: Look) => x.media.find((m) => /\.mp4(\?|$)/.test(m));

function span(x: Look): string {
  const a = asSecs(x.start), b = asSecs(x.end);
  if (a == null) return "";
  return b != null && b - a >= 0.5 ? `${at(a)}–${at(b)}` : at(a);
}

function Said({ who, text, sure, why }: { who: string; text: string | null | undefined; sure?: boolean | null; why?: string | null }) {
  if (!text) return null;
  return (
    <div className="tp-said">
      <span className="who">{who}{sure === false && <em>没把握</em>}</span>
      {/* a model copying words off the screen marks their line breaks with a written \n */}
      <p>{typeset(text.replace(/\\n/g, "\n"))}</p>
      {why && <small>{typeset(why)}</small>}
    </div>
  );
}

/** A look in full: what was asked, what the models were shown, what each answered */
function LookCard({ x, onSeek, onShots }: { x: Look; onSeek: (t: number) => void; onShots: (i: number) => void }) {
  const shots = shotsOf(x);
  const clip = clipOf(x);
  const a = asSecs(x.start);
  return (
    <div className="tp-look">
      <div className="tp-look-h">
        <b>{x.id}</b>
        {a != null && <button type="button" className="tp-c" onClick={() => onSeek(a)} title="从这里播放"><span className="num">{span(x)}</span></button>}
        <span>{MODE[x.mode] || x.mode}{x.round === 2 ? " · 补看" : ""}</span>
      </div>
      <p className="tp-look-q">{typeset(x.question)}</p>
      {clip && <video className="tp-look-clip" src={clip} controls playsInline preload="metadata" />}
      {shots.length > 0 && (
        <div className={`tp-look-strip${shots.length === 1 ? " one" : ""}`}>
          {shots.map((s, i) => (
            <button type="button" key={s.id} onClick={() => onShots(i)} aria-label={`看第 ${i + 1} 张`}>
              <img src={s.src} alt="" loading="lazy" />
              {shots.length > 1 && <span className="num">{at(s.start)}</span>}
            </button>
          ))}
        </div>
      )}
      {x.error && !x.answer ? <p className="tp-look-err">没看成：{errorLabel(x.error)}</p> : (
        <>
          <Said who="回看" text={x.answer} sure={x.sure} why={x.evidence} />
          <Said who="复核" text={x.answer2} sure={x.sure2} why={x.evidence2} />
        </>
      )}
    </div>
  );
}

/** A look in the list of looks: what was asked and answered, at a glance */
function LookRow({ x, on, onOpen }: { x: Look; on: boolean; onOpen: () => void }) {
  const shots = shotsOf(x);
  return (
    <li>
      <button type="button" className={`tp-lr${on ? " on" : ""}`} onClick={onOpen} aria-pressed={on}>
        <span className="tp-lr-h"><b>{x.id}</b><span className="num">{span(x)}</span><span>{MODE[x.mode] || x.mode}</span>{x.answer2 && <span>复核</span>}</span>
        <span className="tp-lr-q">{typeset(x.question)}</span>
        <span className={`tp-lr-a${!x.answer ? " none" : ""}`}>{x.answer ? typeset(x.answer.replace(/\\n/g, " ")) : x.error ? `没看成：${errorLabel(x.error)}` : "—"}</span>
        {shots[0] && <img src={shots[0].src} alt="" loading="lazy" />}
      </button>
    </li>
  );
}

/* ───────────────────────── the draft ───────────────────────── */
/** A line of the 底稿 (topics/draft.py render): a stretch's head, a heading, or a line with what it is — 画面 动作 声音 文字
 *  as the picture model wrote them, 帧 a full-resolution frame's text, 说 what was said — and the moment it starts at */
type DLine = { k: "b"; a: string; head: string } | { k: "h"; text: string }
  | { k: "l"; who: string; a?: string; kind?: "say" | "frame"; text: string[] };
const LABEL = /^(画面|动作|声音|文字|镜头|字幕)[:：]\s*(.*)$/;
const AT = String.raw`\d{1,2}:\d{2}(?::\d{2})?(?:\.\d)?`;

function parseDraft(text: string): DLine[] {
  const out: DLine[] = [];
  const line = (x: Extract<DLine, { k: "l" }>) => {
    const prev = out[out.length - 1];
    // 文字： alone on its line and what it says on the lines under it: the first of them goes beside it
    if (!x.who && prev?.k === "l" && prev.who && !prev.a && !prev.text.length) Object.assign(prev, { a: x.a, kind: x.kind, text: x.text });
    else out.push(x);
  };
  let begun = false;
  for (const raw of text.split("\n")) {
    const l = raw.trim();
    if (!l) continue;
    let m: RegExpMatchArray | null;
    if ((m = l.match(new RegExp(String.raw`^\[(${AT})\s*[-–]\s*${AT}\]\s*(?:类型[:：]\s*)?(.*)$`)))) { begun = true; out.push({ k: "b", a: m[1], head: m[2] }); continue; }
    if (!begun) continue;  // the head: title, link, channel — the page shows them
    if ((m = l.match(/^##\s+(.*)$/))) out.push({ k: "h", text: m[1] });
    else if ((m = l.match(new RegExp(String.raw`^高清帧\s+(${AT})[:：]\s*(.*)$`)))) out.push({ k: "l", who: "帧", a: m[1], kind: "frame", text: m[2].split(" ⏎ ") });
    else if ((m = l.match(new RegExp(String.raw`^说(?:\s+(${AT}))?[:：]\s*(.*)$`)))) out.push({ k: "l", who: "说", a: m[1], kind: "say", text: [m[2]] });
    else if ((m = l.match(LABEL))) out.push({ k: "l", who: m[1], text: m[2] ? [m[2]] : [] });
    else if ((m = l.match(new RegExp(String.raw`^(${AT})(?:\s+|[:：]\s*)(.+)$`)))) line({ k: "l", who: "", a: m[1], kind: "say", text: [m[2]] });
    else line({ k: "l", who: "", text: [l.replace(/^[-*]\s+/, "")] });
  }
  return out;
}

function DraftView({ tid, vid, onCite }: { tid: number; vid: string; onCite: (c: Cite) => void }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    get<{ text: string }>(`/api/topics/${tid}/videos/${vid}/draft`).then((r) => { if (live) setText(r.text); }).catch((e) => { if (live) setErr((e as Error).message); });
    return () => { live = false; };
  }, [tid, vid]);
  const lines = useMemo(() => (text ? parseDraft(text) : []), [text]);
  if (err) return <p className="tp-quiet">{err}</p>;
  if (text == null) return <div className="tp-loading" role="status" aria-label="正在载入"><i /></div>;
  const chip = (a: string, kind: "say" | "frame") => (
    <button type="button" className={`tp-c${kind === "frame" ? " frame" : ""}`} onClick={() => onCite({ t: asSecs(a)!, at: a, kind })} title={kind === "frame" ? "看这一帧" : "从这里播放"}>
      <span className="num">{a}</span>
    </button>
  );
  return (
    <div className="tp-draft">
      {lines.map((l, i) => {
        if (l.k === "b") return <div key={i} className="tp-dr-b">{chip(l.a, "say")}{l.head && <span>{typeset(l.head)}</span>}</div>;
        if (l.k === "h") return <h4 key={i} className="tp-dr-h">{l.text}</h4>;
        return (
          <p key={i} className={`tp-dr-l${l.kind === "frame" ? " f" : l.who === "说" ? " s" : ""}`}>
            <span className="who">{l.who}</span>
            <span>{l.a && chip(l.a, l.kind || "say")}{l.text.map((x, j) => <span key={j} className="ln">{typeset(x)}</span>)}</span>
          </p>
        );
      })}
    </div>
  );
}

/* ───────────────────────── the page ───────────────────────── */
/** What a citation answers with beside the notes: a frame, or a look */
function Evidence({ ev, v, looks, onSeek, onShots }: {
  ev: Ev; v: VView; looks: Look[]; onSeek: (t: number) => void; onShots: (shots: ReturnType<typeof shotsOf>, i: number) => void;
}) {
  if (ev.kind === "look") {
    const x = looks.find((l) => l.id === ev.id);
    if (!x) return <p className="tp-quiet">没有 {ev.id} 这次回看</p>;
    return <LookCard x={x} onSeek={onSeek} onShots={(i) => onShots(shotsOf(x), i)} />;
  }
  if (ev.kind === "frame") {
    const src = `/api/topics/${v.topic.id}/videos/${v.video.id}/frame?t=${ev.t}`;
    return (
      <figure className="tp-ev-frame">
        {v.frames || v.shots.includes(ev.t.toFixed(1)) ? (
          <button type="button" onClick={() => onShots([{ id: `f${ev.t}`, src, start: ev.t, meta: { reason: undefined } }], 0)} aria-label="放大看">
            <img src={src} alt={`${ev.at} 的画面`} />
          </button>
        ) : <p className="tp-quiet">原片已清理，看不了高清帧</p>}
        <figcaption><span className="num">{ev.at}</span>画面<button type="button" className="link" onClick={() => onSeek(ev.t)}>从这里播放</button></figcaption>
      </figure>
    );
  }
  return null;
}

export default function TopicVideo({ tid, vid }: { tid: number; vid: string }) {
  const [live, setLive] = useState(true);
  const { data, error, loadedAt, reload, setData } = usePoll<VView>(`/api/topics/${tid}/videos/${vid}`, live ? 5000 : 0);
  const narrow = useMedia(NARROW);
  const [tab, setTab] = useState<"notes" | "looks" | "draft">("notes");
  const [ev, setEv] = useState<Ev | null>(null);
  const [pinned, setPinned] = useState(false);
  const [box, setBox] = useState<{ shots: ReturnType<typeof shotsOf>; i: number } | null>(null);
  const { say, toast } = useToast();
  const onChip = useRef<Element | null>(null);
  const player = useRef<PlayerHandle>(null);
  const stage = useRef<HTMLElement>(null);
  const notesRef = useRef<HTMLDivElement>(null);
  const first = useRef(true);
  const it = data?.item;
  useEffect(() => { if (it) setLive(it.state === "RUNNING" || it.state === "QUEUED"); }, [it?.state]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (data) document.title = `${data.video.title || vid} · 专题`; }, [data?.video.title]); // eslint-disable-line react-hooks/exhaustive-deps

  const seek = useCallback((t: number, play = true) => {
    player.current?.seek(Math.max(0, t), play);
    if (narrow && play) { setPinned(true); stage.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
  }, [narrow]);
  const cite = useCallback((c: Cite) => {
    if (c.look) setEv({ kind: "look", id: c.look });
    else if (c.t != null && c.kind === "frame") { setEv({ kind: "frame", t: c.t, at: c.at || at(c.t) }); if (!narrow) player.current?.seek(c.t, false); }
    else if (c.t != null) { setEv(null); seek(c.t - 1); }
  }, [narrow, seek]);

  // opened from the answer at a moment, a frame or a look (?t= &k=frame, ?look=)
  useEffect(() => {
    if (!data || !first.current) return;
    first.current = false;
    const q = new URLSearchParams(location.search);
    const look = q.get("look"), t = q.get("t");
    if (look) cite({ look });
    else if (t != null && !isNaN(+t)) cite({ t: +t, at: at(+t), kind: q.get("k") === "frame" ? "frame" : "say" });
  }, [data, cite]);

  // the chip asked about stays marked in the notes, until another is
  const mark = (el: Element | null) => {
    onChip.current?.classList.remove("on");
    onChip.current = el;
    el?.classList.add("on");
  };

  // J / K: the next and the previous video; Esc: back to the topic
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable || document.querySelector("dialog[open]")) return;
      if (e.key === "j" && data?.next) navigate(`/topics/${tid}/${data.next}`, { replace: true });
      else if (e.key === "k" && data?.prev) navigate(`/topics/${tid}/${data.prev}`, { replace: true });
      else if (e.key === "Escape") navigate(`/topics/${tid}`);
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [data?.next, data?.prev, tid]); // eslint-disable-line react-hooks/exhaustive-deps

  const html = useMemo(() => (data?.notes ? renderCited(data.notes) : ""), [data?.notes]);
  if (!data) {
    return <main className="page tp-video">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  }
  const v = data, item = v.item, s = itemState(item, v.topic.paused ? "paused" : v.held ? "held" : undefined);
  const act = async (action: string, ok?: string, why?: string) => {
    try {
      await post(`/api/topics/${tid}/videos/${vid}`, { action, why });
      if (action === "remove") { handoff.out = { tid, vid, no: item.no }; navigate(`/topics/${tid}`, { replace: true }); return; }
      setData(await get<VView>(`/api/topics/${tid}/videos/${vid}`));
      setLive(true);
      if (ok) say({ text: ok });
    } catch (e) { say({ text: (e as Error).message, bad: true }); }
  };
  const onNotes = (e: React.MouseEvent) => {
    const c = citeOf(e.target as Element);
    if (!c) return;
    mark((e.target as Element).closest(".tp-c"));
    cite(c);
  };
  const done = item.state === "DONE";
  const none = done && item.fit === "none";
  const retry = !item.excluded && (item.state === "FAILED" || (item.state === "WAITING" && item.wait !== "TOPIC_QUOTA"));
  const evBody = ev && ev.kind !== "say" ? (
    <Evidence ev={ev} v={v} looks={v.looks} onSeek={(t) => { if (narrow) setEv(null); seek(t); }} onShots={(shots, i) => setBox({ shots, i })} />
  ) : null;
  const meta = [v.video.channel, len(v.video.duration), pubDate(v.video.published)].filter(Boolean);

  return (
    <main className="page tp-video">
      <Stale error={error} loadedAt={loadedAt} />
      <header className="tp-vhead">
        <div className="tp-vnav">
          <a className="crumb" {...linkProps(`/topics/${tid}`)}><span aria-hidden>←</span><span className="tp-crumb-q">{v.topic.question}</span></a>
          <span className="tp-step">
            <span className="num">V{item.no}</span>
            <a className={`iconbtn${v.prev ? "" : " off"}`} aria-label="上一期" aria-disabled={!v.prev} {...(v.prev ? linkProps(`/topics/${tid}/${v.prev}`) : { href: undefined })} title="上一期（K）">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M15 5l-7 7 7 7" /></svg>
            </a>
            <a className={`iconbtn${v.next ? "" : " off"}`} aria-label="下一期" aria-disabled={!v.next} {...(v.next ? linkProps(`/topics/${tid}/${v.next}`) : { href: undefined })} title="下一期（J）">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M9 5l7 7-7 7" /></svg>
            </a>
          </span>
        </div>
        <h1><Words>{v.video.title || vid}</Words></h1>
        <p className="tp-vmeta">
          {meta.map((m, i) => <span key={i}>{m}</span>)}
          {v.video.gone && <span>{v.backup?.restored ? "原片已下架 · 网盘备份" : "原片已下架"}</span>}
          <a href={v.video.url} target="_blank" rel="noreferrer">YouTube ↗</a>
        </p>
        <div className="tp-vhead-b">
          <div className="tp-verdict">
            {done && !item.excluded ? <span className={`tp-tag ${item.fit}`}>{FIT[item.fit || "some"]}</span>
              : <span className={`tp-st ${s.tone || ""}`}>{item.state !== "FAILED" && !item.excluded && <Phases it={item} />}{s.text}</span>}
            {item.gist && <p>{typeset(item.gist)}</p>}
          </div>
          <div className="tp-acts">
            {none && !item.force && <button className="btn sm" onClick={() => act("force", "照样读这期")}>仍然读</button>}
            {retry && <button className="btn sm" onClick={() => act("retry", "重新排上")}>重试</button>}
            {item.excluded ? <button className="btn ghost sm" onClick={() => act("include", "算进回答")}>算这期</button>
              : <LeaveOut onGo={(why) => act("exclude", `不算：${why}`, why)} />}
            <Armed label="移出专题" sure="确认移出" onGo={() => act("remove")} />
          </div>
        </div>
      </header>

      <div className="tp-vbody">
        <aside ref={stage} className={`tp-stage${pinned ? " pinned" : ""}`} aria-label="视频">
          <Player ref={player} vid={vid} local={v.playback} title={v.video.title || vid} />
          {pinned && narrow && (
            <button type="button" className="tp-unpin" onClick={() => setPinned(false)} aria-label="不再固定">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden><path d="M6 6l12 12M18 6 6 18" /></svg>
            </button>
          )}
          {!narrow && evBody && <div className="tp-ev">{evBody}</div>}
        </aside>
        <div className="tp-vmain">
          <div className="seg tp-tabs" role="tablist" aria-label="看什么">
            <button role="tab" aria-selected={tab === "notes"} className={tab === "notes" ? "on" : ""} onClick={() => setTab("notes")}>笔记</button>
            <button role="tab" aria-selected={tab === "looks"} className={tab === "looks" ? "on" : ""} onClick={() => setTab("looks")} disabled={!v.looks.length}>回看{v.looks.length ? ` ${v.looks.length}` : ""}</button>
            <button role="tab" aria-selected={tab === "draft"} className={tab === "draft" ? "on" : ""} onClick={() => setTab("draft")} disabled={!v.draft}>底稿</button>
          </div>
          {tab === "notes" && (
            <div className="tp-vnotes" ref={notesRef} onClick={onNotes}>
              {item.stale && <p className="tp-was">按新问题重读中，下面是上一版</p>}
              {v.notes ? <div className="prose" dangerouslySetInnerHTML={{ __html: html }} />
                : none ? (
                  <div className="tp-none">
                    <h3>这期和问题无关</h3>
                    {v.plan?.reason && <p>{typeset(v.plan.reason)}</p>}
                  </div>
                ) : item.state === "FAILED" ? (
                  <div className="tp-none bad"><h3>{failText(item.error_class)}</h3>{item.error && <p>{typeset(item.error.slice(0, 200))}</p>}</div>
                ) : (
                  <div className="tp-none" role="status">
                    <i className="tp-rule" aria-hidden />
                    <h3>{item.state === "RUNNING" ? `正在${STEP[item.step || ""] || "读"}` : s.text}</h3>
                    <p>{item.state === "RUNNING" ? "底稿、回看、写作，读完笔记在这里" : ""}</p>
                  </div>
                )}
            </div>
          )}
          {tab === "looks" && (
            <ol className="tp-lrs">
              {v.looks.map((x) => <LookRow key={`${x.round}-${x.id}`} x={x} on={ev?.kind === "look" && ev.id === x.id} onOpen={() => setEv({ kind: "look", id: x.id })} />)}
            </ol>
          )}
          {tab === "draft" && <DraftView tid={tid} vid={vid} onCite={cite} />}
        </div>
      </div>
      {narrow && evBody && <Sheet title={ev?.kind === "look" ? ev.id : "画面"} onClose={() => { setEv(null); mark(null); }} className="tp-sheet">{evBody}</Sheet>}
      {box && <Lightbox shots={box.shots} index={box.i} onClose={() => setBox(null)} />}
      {toast}
    </main>
  );
}
