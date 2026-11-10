import DOMPurify from "dompurify";
import { marked, type Token, type Tokens } from "marked";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { media, post, srcSet } from "../api";
import { cleanUnitTitle, cnNo, deJargon, endStop, issueHeadline, originalTitle, quotedFragments, typeset, Words } from "../cjk";
import { Fold } from "../components/Fold";
import { Lightbox } from "../components/Lightbox";
import { Sheet } from "../components/Sheet";
import { Fault, Stale, Unroll, Empty } from "../components/States";
import { useLeaving, useMedia, usePoll, useStored } from "../hooks";
import { linkProps, useRestoreScroll } from "../router";
import { cnDay, cnNum, cnYear, deliveryLabel, failText, mins, parseDay, reduceMotion, tc } from "../util";

marked.setOptions({ gfm: true, breaks: false });

type Unit = any;
type Asset = any;
type Flag = any;
type Active = { assetId: number; unitId: string; title: string; time: number; docked: boolean; clipStart: number; seq: number } | null;

const FLAG_KIND: Record<string, string> = {
  BOUNDARY_FALLBACK: "边界兜底", BOUNDARY_UNCERTAIN: "边界", CONTEXT_MISSING: "上下文",
  CLIP_TOO_SHORT: "片段过短", MEDIA_QA_FAILED: "媒体质检", UNIT_DETAIL_MISSING: "内容缺失", SECTION_MISSING: "正文缺失",
  EDITORIAL_TERM: "措辞", MERGE_CONFLICT: "分段矛盾", OVERSTATEMENT_CANDIDATE: "程度", CLAIM_MISMATCH: "陈述核对",
  ATTRIBUTION_OR_CONDITION_REVIEW_REQUIRED: "条件与归属", CLIP_CONTEXT_REVIEW_REQUIRED: "上下文",
  PROVIDER_OUTPUT_LIMIT: "模型输出上限", PARSER_FAILURE: "解析恢复", STRUCTURED_CONTRACT_FAILURE: "结构化失败",
  ORCHESTRATION_FAILURE: "编排", SOURCE_TIMESTAMP_OVERRUN: "时间越界", EDITORIAL_REVIEW_CANDIDATE: "措辞",
  ANCHOR_OUT_OF_RANGE: "截图锚点", HALF_SENTENCE_START: "片段起点", NEXT_TOPIC_BLEED: "片段终点", ENDS_BEFORE_CONCLUSION: "片段终点",
  DANGLING_REFERENCE: "指代", ATTRIBUTION_RISK: "归属", OVERSTATEMENT: "程度", SOFTENED: "改轻", CLIP_SHARED: "共用片段", UNIT_SPLIT_SPAN: "单元区间",
  CLIP_TOO_LONG: "片段过长", SELECTION_FALLBACK: "选片兜底", MERGE_UNREFERENCED_TOPIC: "合并", SCREENSHOT_FAILED: "截图",
  BAD_TIMECODE: "时间码", CLIP_RENDER_FAILED: "剪辑失败", SAFETY_FALLBACK: "换模型",
};
const flagKind = (k: string) => FLAG_KIND[k] || FLAG_KIND[k.replace(/^AI_/, "")] || "其他";
const RESOLVED: Record<string, string> = { ACCEPTED: "无误", FIXED: "已改好", DISMISSED: "已忽略" };
const isReview = (f: Flag) => f.severity !== "INFO";
const isOpen = (f: Flag) => f.resolution === "OPEN" && isReview(f);

/* ───────────────────────────── markdown ───────────────────────────── */
const NAV_HEAD = /学习导航|优先学习清单|学习清单/;
const NATIVE_FIELD = /^(tickers?|视频时间|时间|推荐截图位置|截图位置|截图)$/i;

/** Typeset, then drop what the page already shows natively: the LLM's navigation tables and
 *  per-unit fields (tickers, time, screenshot hints, facets rendered below). */
function renderMd(src: string, dropFields: string[] = []): string {
  const tokens = marked.lexer(typeset(src || ""));
  const out: Token[] = [];
  let skip = 0;
  for (const t of tokens) {
    if (t.type === "heading") {
      const h = t as Tokens.Heading;
      if (skip && h.depth <= skip) skip = 0;
      const text = h.text.replace(/ /g, "").trim().replace(/[？?：:]$/, "");
      if (NAV_HEAD.test(text) || (h.depth >= 3 && (NATIVE_FIELD.test(text) || dropFields.some((f) => text.startsWith(f))))) {
        skip = h.depth;
        continue;
      }
    }
    if (skip) continue;
    if (t.type === "table") {
      const head = (t as Tokens.Table).header.map((c) => c.text).join("|");
      if (/单元|知识点/.test(head) && /看点|优先|为什么/.test(head)) continue;
    }
    out.push(t);
  }
  (out as any).links = (tokens as any).links;
  return DOMPurify.sanitize(marked.parser(out as any) as string);
}

/* 圈点: wrap the questioned sentence in the prose so the note lands on the words it is about */
function wrapText(root: HTMLElement, needle: string, id: number): HTMLElement | null {
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode() as Text | null; n; n = w.nextNode() as Text | null) {
    if ((n.parentElement as HTMLElement)?.closest("em.quan")) continue;
    const i = n.data.indexOf(needle);
    if (i < 0) continue;
    const r = document.createRange();
    r.setStart(n, i);
    r.setEnd(n, i + needle.length);
    const em = document.createElement("em");
    em.className = "quan";
    em.dataset.flag = String(id);
    r.surroundContents(em);
    return em;
  }
  return null;
}

function needleCandidates(f: Flag): string[] {
  const c: string[] = [];
  if (f.evidence?.claim) c.push(f.evidence.claim);
  c.push(...quotedFragments(f.body || ""));
  c.push(...quotedFragments((f.title || "").replace(/^「[^」]*」/, "")));
  return c.map((x) => typeset(x.replace(/[*_`#>]/g, "").trim())).filter((x) => x.length >= 4);
}

/** the note's headline without the unit title it repeats: 「MRNA…」程度可能被加强 → 程度可能被加强 */
const verbOf = (f: Flag) => (f.title || "").replace(/^「[^」]*」\s*/, "").trim() || f.title;
/** older notes carry a category-only title (「单元」+ 类别); the pipeline now writes a specific one */
const GENERIC = /^「[^」]*」\s*(片段|程度|陈述|归属|开头|结尾|边界|截图)/;
function firstClause(s: string) {
  const t = deJargon(s || "").split(/[。；]/)[0].trim();
  if (t.length <= 40) return t;
  const cut = t.lastIndexOf("，", 40);
  return (cut > 12 ? t.slice(0, cut) : t.slice(0, 38)) + "…";
}
/** the body only says the title again: a section that did not pass its check, the title being the sentence cut at 40 */
const echoes = (f: Flag) => !!f.body && f.body.trim().startsWith(verbOf(f));
function headOf(f: Flag, needle?: string) {
  if (echoes(f)) return (f.title || "").replace(verbOf(f), () => f.body!.trim());
  if (!GENERIC.test(f.title || "")) return f.title || "";
  const verb = verbOf(f);
  if (needle) return `「${needle}」${verb.replace(/^片段/, "")}`;
  return (f.body && firstClause(f.body)) || verb;
}

/* near-match 圈点: the sentence in one text node whose character bigrams best cover a candidate (≥ 0.6) */
const bigrams = (s: string) => {
  const t = s.replace(/[^\p{Script=Han}A-Za-z0-9]/gu, "");
  const o = new Set<string>();
  for (let i = 0; i < t.length - 1; i++) o.add(t.slice(i, i + 2));
  return o;
};
const overlap = (a: Set<string>, b: Set<string>) => { let n = 0; a.forEach((x) => { if (b.has(x)) n++; }); return n / Math.max(1, Math.min(a.size, b.size)); };
function wrapBest(root: HTMLElement, cands: string[], id: number): HTMLElement | null {
  const cb = cands.map(bigrams).filter((b) => b.size >= 4);
  if (!cb.length) return null;
  let best: { n: Text; i: number; len: number; s: number } | null = null;
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode() as Text | null; n; n = w.nextNode() as Text | null) {
    if ((n.parentElement as HTMLElement)?.closest("em.quan")) continue;
    for (const m of n.data.matchAll(/[^。！？；]{8,}[。！？；]?/g)) {
      const mb = bigrams(m[0]);
      for (const c of cb) {
        const sc = overlap(c, mb);
        if (sc >= 0.6 && (!best || sc > best.s)) best = { n, i: m.index!, len: m[0].length, s: sc };
      }
    }
  }
  if (!best) return null;
  const r = document.createRange();
  r.setStart(best.n, best.i);
  r.setEnd(best.n, best.i + best.len);
  const em = document.createElement("em");
  em.className = "quan near";
  em.dataset.flag = String(id);
  r.surroundContents(em);
  return em;
}

/* ───────────────────────────── spine (desktop): 书口 with 鱼尾 ───────────────────────────── */
function Spine({ title, duration, units, flags, missing, current, playhead, onJump }: {
  title: string; duration: number; units: Unit[]; flags: Flag[]; missing: Set<string>; current: string | null;
  playhead: number | null; onJump: (id: string) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 76, h: 600 });
  const [hover, setHover] = useState<{ u: Unit; i: number; y: number } | null>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);
  const { w, h } = size;
  const y = (t: number) => (Math.min(duration, Math.max(0, t)) / duration) * h;
  const openBy: Record<string, number> = {};
  flags.filter(isOpen).forEach((f) => { if (f.target_id) openBy[f.target_id] = (openBy[f.target_id] || 0) + 1; });
  const ticks: number[] = [];
  for (let t = 600; t < duration - 120; t += 600) ticks.push(t);
  const ci = units.findIndex((u) => u.id === current);
  const act = ci >= 0 ? units[ci] : null;

  return (
    <div className="spine">
      <div className="spine-title" aria-hidden>{title}</div>
      <div className="spine-track" ref={box} onMouseLeave={() => setHover(null)}>
        <svg width={w} height={h} role="group" aria-label={`整期时间轴：${mins(duration)}，${units.length} 个知识单元`}>
          <line x1="21" x2="21" y1="0" y2={h} className="sp-axis" />
          <text x="16" y="9" textAnchor="end" className="sp-lbl">0′</text>
          {ticks.map((t) => (
            <g key={t}>
              <line x1="17" x2="21" y1={y(t)} y2={y(t)} className="sp-tick" />
              <text x="14" y={y(t) + 3.5} textAnchor="end" className="sp-lbl">{Math.round(t / 60)}′</text>
            </g>
          ))}
          <text x="16" y={h} textAnchor="end" className="sp-lbl">{mins(duration)}</text>
          {units.map((u, i) => {
            const y0 = y(u.start), y1 = y(u.end);
            const hh = Math.max(2, y1 - y0 - 2);
            const hollow = missing.has(u.id);
            return (
              <a key={u.id} href={`#u-${u.id}`} aria-label={`${cnNo(i + 1)} ${u.title} · ${tc(u.start)}`}
                tabIndex={i === Math.max(0, ci) ? 0 : -1}
                onKeyDown={(e) => {
                  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
                  e.preventDefault();
                  const k = Math.max(0, Math.min(units.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)));
                  onJump(units[k].id);
                  requestAnimationFrame(() => (box.current?.querySelectorAll("a")[k] as HTMLElement | undefined)?.focus());
                }}
                onClick={(e) => { e.preventDefault(); onJump(u.id); }}
                onMouseEnter={() => setHover({ u, i, y: (y0 + y1) / 2 })} onFocus={() => setHover({ u, i, y: (y0 + y1) / 2 })} onBlur={() => setHover(null)}>
                <rect x="0" y={y0} width={w} height={Math.max(6, y1 - y0)} fill="transparent" />
                <rect x={hollow ? 24.5 : 24} y={y0 + 1} width={hollow ? 7 : 8} height={hh} rx="1"
                  className={`sp-seg${u.learning_priority === 3 ? " must" : ""}${u.id === current ? " on" : ""}${hollow ? " hollow" : ""}`} />
                {Array.from({ length: Math.min(openBy[u.id] || 0, 4) }, (_, k) => (
                  <rect key={k} x="35" y={y0 + 2 + k * 4} width="6" height="2" className="sp-pi" />
                ))}
              </a>
            );
          })}
          {playhead != null && <line x1="18" x2="35" y1={y(playhead)} y2={y(playhead)} className="sp-head" />}
          {act && (
            <g className="sp-fish" style={{ transform: `translate(44px, ${y(act.start) - 1}px)` }} aria-hidden>
              <path d="M0 0H11V11L5.5 6.5L0 11Z" />
            </g>
          )}
        </svg>
        {hover && (
          <div className="sp-tip" style={{ top: Math.min(h - 70, Math.max(70, hover.y)) }}>
            <span className="num">{cnNo(hover.i + 1)} · {tc(hover.u.start)}{hover.u.learning_priority === 3 ? " · 必学" : ""}</span>
            <Words>{hover.u.title}</Words>
          </div>
        )}
      </div>
    </div>
  );
}

/* ───────────────────────────── shukou (phone): a 14px book-edge you can tap or drag ───────────────────────────── */
function Shukou({ duration, units, flags, current, onJump }: {
  duration: number; units: Unit[]; flags: Flag[]; current: string | null; onJump: (id: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ x: number; i: number } | null>(null);
  const flagged = new Set(flags.filter(isOpen).map((f) => f.target_id));
  const pct = (t: number) => (Math.min(duration, Math.max(0, t)) / duration) * 100;
  const ci = Math.max(0, units.findIndex((u) => u.id === current));
  const pick = (clientX: number) => {
    const r = ref.current!.getBoundingClientRect();
    const t = ((clientX - r.left) / r.width) * duration;
    let i = units.findIndex((u) => u.end >= t);
    if (i < 0) i = units.length - 1;
    return { x: Math.min(r.width - 8, Math.max(8, clientX - r.left)), i };
  };
  return (
    <div className="shukou" ref={ref} role="slider" tabIndex={0} aria-label="本期位置"
      aria-valuemin={1} aria-valuemax={units.length} aria-valuenow={ci + 1} aria-valuetext={units[ci]?.title}
      onPointerDown={(e) => { (e.target as HTMLElement).setPointerCapture?.(e.pointerId); setDrag(pick(e.clientX)); }}
      onPointerMove={(e) => { if (drag) setDrag(pick(e.clientX)); }}
      onPointerUp={() => { if (drag) onJump(units[drag.i].id); setDrag(null); }}
      onPointerCancel={() => setDrag(null)}
      onKeyDown={(e) => {
        if (e.key === "ArrowRight") onJump(units[Math.min(units.length - 1, ci + 1)].id);
        if (e.key === "ArrowLeft") onJump(units[Math.max(0, ci - 1)].id);
      }}>
      <div className="sk-track" aria-hidden>
        {units.map((u) => (
          <i key={u.id} className={u.learning_priority === 3 ? "must" : ""} style={{ left: `${pct(u.start)}%`, width: `${Math.max(0.4, pct(u.end) - pct(u.start))}%` }} />
        ))}
        {units.filter((u) => flagged.has(u.id)).map((u) => <b key={u.id} style={{ left: `${pct(u.start)}%` }} />)}
        {units[ci] && <span className="yuwei" style={{ left: `${pct(units[ci].start)}%` }} />}
      </div>
      {drag && (
        <div className="sk-bubble" style={{ left: drag.x }}>
          <span className="num">{cnNo(drag.i + 1)}</span> <Words>{units[drag.i].title}</Words>
        </div>
      )}
    </div>
  );
}

/* ───────────────────────────── 要目 ───────────────────────────── */
function IssueIndex({ units, flags, current, onJump, mobile, full = false }: {
  units: Unit[]; flags: Flag[]; current: string | null; onJump: (id: string) => void; mobile: boolean; full?: boolean;
}) {
  const [allPref, setAll] = useStored("az-idx-all", false);
  const all = full || allPref;
  useEffect(() => {
    // the sheet opens its <dialog> in a parent effect, after this one: wait a frame
    if (full && current) requestAnimationFrame(() => document.querySelector(`.sheet .yaomu-row[href="#u-${current}"]`)?.scrollIntoView({ block: "center" }));
  }, [full]); // eslint-disable-line react-hooks/exhaustive-deps
  const [open, setOpen] = useState(!mobile);
  useEffect(() => setOpen(!mobile), [mobile]);
  const openBy: Record<string, number> = {};
  flags.filter(isOpen).forEach((f) => { if (f.target_id) openBy[f.target_id] = (openBy[f.target_id] || 0) + 1; });
  const must = units.filter((u) => u.learning_priority === 3).length;
  const pi = Object.values(openBy).reduce((a, b) => a + b, 0);
  const rows = units.map((u, i) => ({ u, i })).filter(({ u }) => all || u.learning_priority === 3);
  return (
    <nav className={`yaomu${open ? " open" : ""}`} aria-label="要目">
      <button className="yaomu-head" onClick={() => setOpen(!open)} aria-expanded={open} disabled={!mobile}>
        <h2>要目</h2>
        {mobile && <span className="yaomu-sum"><span className="num">{units.length}</span> 单元 · 必学 <span className="num">{must}</span>{pi > 0 && <> · 校注 <span className="num">{pi}</span></>}</span>}
        {mobile && <i className={`chev${open ? " up" : ""}`} aria-hidden />}
      </button>
      <Fold open={open}>
        <ol>
          {rows.map(({ u, i }) => (
            <li key={u.id}>
              <a href={`#u-${u.id}`} className={`yaomu-row${u.id === current ? " cur" : ""}${u.learning_priority === 3 ? " must" : ""}`}
                onClick={(e) => { e.preventDefault(); onJump(u.id); }}>
                <span className="no">{cnNo(i + 1)}</span>
                <span className="t"><span className="tt"><Words>{u.title}</Words></span></span>
                <span className="tc num">{tc(u.start)}</span>
                {openBy[u.id] ? <span className="pi">校 <span className="num">{openBy[u.id]}</span></span> : <span className="pi none" />}
              </a>
            </li>
          ))}
        </ol>
        {!full && must < units.length && (
          <button className="yaomu-more" onClick={() => setAll(!all)}>
            {all ? "只看必学" : `其余 ${units.length - must} 个单元`}<i className={`chev${all ? " up" : ""}`} aria-hidden />
          </button>
        )}
      </Fold>
    </nav>
  );
}

/* ───────────────────────────── video ───────────────────────────── */
function Film({ unit, clip, sub, poster, active, setActive, onTime, eager }: {
  unit: Unit; clip?: Asset; sub?: Asset; poster?: Asset; active: Active; setActive: (a: Active) => void;
  onTime: (t: number | null) => void; eager: boolean;
}) {
  const vid = sub && sub.status !== "FAILED" ? sub : clip;
  const ref = useRef<HTMLDivElement>(null);
  const vref = useRef<HTMLVideoElement>(null);
  const [ready, setReady] = useState(false);
  const isActive = !!active && !!vid && active.assetId === vid.id;
  useEffect(() => {
    if (!isActive || !ref.current) return;
    const io = new IntersectionObserver(([e]) => {
      const v = vref.current;
      if (!e.isIntersecting && v && !v.paused) {
        setActive({ ...active!, time: v.currentTime, docked: true });
      } else if (e.isIntersecting && active?.docked) {
        setActive({ ...active, docked: false });
      }
    }, { threshold: 0.25 });
    io.observe(ref.current);
    return () => io.disconnect();
  }, [isActive, active, setActive]);
  useEffect(() => {
    if (isActive && !active!.docked && vref.current) {
      vref.current.currentTime = active!.time || 0;
      vref.current.play().catch(() => {});
    }
  }, [isActive, active?.docked, active?.seq]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!clip) return null;
  if (clip.status === "NOT_SELECTED" && clip.meta?.shared_with) {
    return <div className="film-note">这一段与「{clip.meta.shared_title}」落在同一段视频里，片段见该单元。</div>;
  }
  if (clip.status !== "GENERATED") {
    return <div className="film-note">这一段的视频片段没有生成：{clip.meta?.error || "原因见校注"}。文字与截图照常保留。</div>;
  }
  const secs = Math.round(clip.end - clip.start);
  return (
    <div className={`film${ready ? " ready" : ""}`} ref={ref}>
      {isActive && !active!.docked ? (
        <video ref={vref} src={media(vid.id)} controls autoPlay playsInline preload="auto"
          onTimeUpdate={(e) => onTime(clip.start + (e.target as HTMLVideoElement).currentTime)}
          onPause={() => onTime(null)} />
      ) : (
        <>
          {poster && <img src={media(poster.id, 1280)} srcSet={srcSet(poster.id)} sizes="(max-width: 900px) 100vw, 640px" alt="" loading={eager ? "eager" : "lazy"} fetchPriority={eager ? "high" : "auto"}
            onLoad={() => setReady(true)} onError={(e) => { (e.target as HTMLImageElement).style.visibility = "hidden"; }} />}
          <button className="play" onClick={() => setActive({ assetId: vid.id, unitId: unit.id, title: unit.title, time: 0, docked: false, clipStart: clip.start, seq: Date.now() })}
            aria-label={`播放：${unit.title}`}>
            <span className="disc">
              {isActive ? <span className="disc-txt">小窗中</span> :
                <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden><path d="M8 5.5v13l11-6.5-11-6.5Z" fill="currentColor" /></svg>}
            </span>
          </button>
          <div className="cap">
            <span className="num">{secs} 秒</span>
            {sub && sub.status !== "FAILED" && <span>字幕</span>}
            {!clip.standalone && <span>依赖前文</span>}
          </div>
        </>
      )}
    </div>
  );
}

function Dock({ active, setActive, onBack, onTime }: { active: Active; setActive: (a: Active) => void; onBack: () => void; onTime: (t: number | null) => void }) {
  const vref = useRef<HTMLVideoElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [paused, setPaused] = useState(false);
  const { shown, leaving } = useLeaving(active?.docked ? active : null);
  useEffect(() => {
    if (vref.current && active?.docked) { vref.current.currentTime = active.time; vref.current.play().catch(() => {}); }
  }, [active?.assetId, active?.docked, active?.seq]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (leaving) vref.current?.pause(); }, [leaving]);  // the clip goes on in the page, not twice
  if (!shown) return null;
  return (
    <div className={`dock${expanded ? " open" : ""}${leaving ? " leaving" : ""}`} role="region" aria-label="小窗播放" inert={leaving}>
      <video ref={vref} src={media(shown.assetId)} playsInline controls={expanded}
        onClick={() => setExpanded(!expanded)}
        onPlay={() => setPaused(false)} onPause={() => { setPaused(true); onTime(null); }}
        onTimeUpdate={(e) => { shown.time = (e.target as HTMLVideoElement).currentTime; onTime(shown.clipStart + shown.time); }} />
      <div className="bar">
        <button className="dock-title" onClick={onBack}><Words>{shown.title}</Words></button>
        <button className="dock-btn" onClick={() => { const v = vref.current; if (v) (v.paused ? v.play() : v.pause()); }} aria-label={paused ? "播放" : "暂停"}>
          {paused ? <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden><path d="M8 5.5v13l11-6.5-11-6.5Z" fill="currentColor" /></svg>
            : <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" fill="currentColor" /></svg>}
        </button>
        <button className="dock-btn" onClick={() => { onTime(null); setActive(null); }} aria-label="关闭小窗">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden><path d="M6 6l12 12M18 6 6 18" /></svg>
        </button>
      </div>
    </div>
  );
}

/* ───────────────────────────── 校注 ───────────────────────────── */
function Gou({ play }: { play: boolean }) {
  return (
    <svg className={`gou${play ? " play" : ""}`} width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path pathLength="1" d="M3 13.5c2.2 1.4 3.6 3 4.6 5 2.8-6.3 7.4-11 13.4-14.5" fill="none" stroke="var(--zhu)" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

function Zhupi({ f, needle, onSet, playAt, focused, compact }: {
  f: Flag; needle?: string; onSet: (f: Flag, r: string) => void; playAt: (unitId: string, t: number) => boolean;
  focused: boolean; compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [fresh, setFresh] = useState(false);
  const [more, setMore] = useState(false);
  useEffect(() => { if (focused) setOpen(true); }, [focused]);
  const resolved = f.resolution !== "OPEN";
  const info = f.severity === "INFO";
  const head = info ? f.title : headOf(f, needle);
  const ev = f.evidence || {};
  const long = (ev.quote || "").length > 70;
  const src = f.origin === "AI_CHECK" ? "AI 预审" : f.origin === "MACHINE_QA" ? "机检" : "";
  const quan = (on: boolean) => document.querySelectorAll(`em.quan[data-flag="${f.id}"]`).forEach((e) => e.classList.toggle("focus", on));
  const set = (r: string) => { setFresh(r !== "OPEN"); onSet(f, r); };

  if (resolved && !info) {
    return (
      <div className="zhupi done" data-flag={f.id}>
        <span className="done-line"><Gou play={fresh} /> {RESOLVED[f.resolution] || "已处理"}<span className="sep">·</span>
          <button className="link" onClick={() => set("OPEN")}>撤回</button></span>
        <span className="done-h"><Words>{head}</Words></span>
      </div>
    );
  }
  return (
    <div className={`zhupi${info ? " info" : ""}${focused ? " flash" : ""}${compact ? " compact" : ""}${compact && !open ? " shut" : ""}`} data-flag={f.id}
      onMouseEnter={() => quan(true)} onMouseLeave={() => quan(false)}>
      {compact ? (
        <button className="zp-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          <span className="k"><i className="pi-mark" aria-hidden>校</i><span>{flagKind(f.kind)}</span>{src && <span className="src">{src}</span>}<i className={`chev${open ? " up" : ""}`} aria-hidden /></span>
          <h5><Words>{head}</Words></h5>
        </button>
      ) : (
        <>
          <div className="k"><i className="pi-mark" aria-hidden>{info ? "注" : "校"}</i><span>{flagKind(f.kind)}</span>{src && <span className="src">{src}</span>}</div>
          <h5><Words>{head}</Words></h5>
        </>
      )}
      <div className="zp-body">
        <div>
          {f.body && (info || !echoes(f)) && <p>{typeset(endStop(deJargon(f.body)))}</p>}
          {ev.quote && (
            <blockquote className={`q${long ? (more ? " open" : " clamp") : ""}`} onClick={long && !more ? () => setMore(true) : undefined}>
              {ev.t != null ? (
                <button className="ev" onClick={() => playAt(f.target_id, ev.t)} aria-label={`从 ${tc(ev.t)} 播放原话`}>
                  <svg width="10" height="10" viewBox="0 0 24 24" aria-hidden><path d="M6 4v16l14-8z" fill="currentColor" /></svg>
                  <span className="num">{tc(ev.t)}</span>
                </button>
              ) : null}
              <span className="q-text">「{typeset(ev.quote)}」</span>
            </blockquote>
          )}
          {ev.start && ev.end && !ev.quote && <p className="ev-range num">{ev.start} – {ev.end}</p>}
          <div className="acts">
            {info ? (
              resolved ? <button className="btn sm ghost" onClick={() => set("OPEN")}>撤回</button>
                : <button className="btn sm ghost" onClick={() => set("DISMISSED")}>知道了</button>
            ) : (
              <>
                <button className="btn sm solid" onClick={() => set("ACCEPTED")}>无误</button>
                <button className="btn sm" onClick={() => set("FIXED")}>我已改好</button>
                <button className="btn sm" onClick={() => set("DISMISSED")}>忽略</button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────────── minor edit: boundary nudge ───────────────────────────── */
function Nudge({ sourceId, clip, onDone }: { sourceId: string; clip: Asset; onDone: () => void }) {
  const [start, setStart] = useState<number>(clip.start);
  const [end, setEnd] = useState<number>(clip.end);
  const [words, setWords] = useState<{ head: any[]; tail: any[] } | null>(null);
  const [state, setState] = useState<string | null>(null);
  useEffect(() => {
    const q = (a: number, b: number) => fetch(`/api/sources/${encodeURIComponent(sourceId)}/words?start=${a}&end=${b}`).then((r) => r.json());
    Promise.all([q(clip.start - 8, clip.start + 6), q(clip.end - 6, clip.end + 8)])
      .then(([h, t]) => setWords({ head: h.words, tail: t.words }))
      .catch(() => setWords({ head: [], tail: [] }));
  }, [sourceId, clip.start, clip.end]);
  const submit = async () => {
    try { await post(`/api/assets/${clip.id}/nudge`, { start, end }); setState("已提交，机房空闲时重剪（约 1 分钟）"); setTimeout(onDone, 1500); }
    catch (e) { setState(String((e as Error).message)); }
  };
  const strip = (ws: any[], kind: "start" | "end") => (
    <div className="strip">
      {ws.length === 0 && <span className="muted">这一期没有全片转写，只能按秒微调。</span>}
      {ws.map((w, i) => {
        const at = kind === "start" ? w.start - 0.15 : w.end + 0.3;
        const cur = kind === "start" ? start : end;
        const inside = kind === "start" ? w.start >= start : w.end <= end;
        const caret = kind === "start" ? Math.abs(w.start - 0.15 - cur) < 0.2 : Math.abs(w.end + 0.3 - cur) < 0.2;
        return (
          <button key={i} className={`w ${inside ? "in" : ""} ${caret ? "caret-" + kind : ""}`} title={tc(w.start)}
            onClick={() => (kind === "start" ? setStart(at) : setEnd(at))}>{w.word}</button>
        );
      })}
    </div>
  );
  return (
    <div className="nudge" role="group" aria-label="微调片段边界">
      <div className="nudge-row">
        <span className="label">起点</span>
        <span className="num">{tc(start)}</span>
        <div className="step"><button onClick={() => setStart(start - 1)}>−1 秒</button><button onClick={() => setStart(start + 1)}>+1 秒</button></div>
      </div>
      {words && strip(words.head, "start")}
      <div className="nudge-row">
        <span className="label">终点</span>
        <span className="num">{tc(end)}</span>
        <div className="step"><button onClick={() => setEnd(end - 1)}>−1 秒</button><button onClick={() => setEnd(end + 1)}>+1 秒</button></div>
      </div>
      {words && strip(words.tail, "end")}
      <div className="nudge-foot">
        <span className="muted">{Math.round(end - start)} 秒</span>
        <button className="btn solid sm" onClick={submit} disabled={end - start < 3}>按此重剪</button>
      </div>
      {state && <p className="zhu nudge-msg">{state}</p>}
    </div>
  );
}

function Switch({ on, onChange, children }: { on: boolean; onChange: (v: boolean) => void; children: React.ReactNode }) {
  return (
    <button role="switch" aria-checked={on} className={`switch${on ? " on" : ""}`} onClick={() => onChange(!on)}>
      <i aria-hidden />{children}
    </button>
  );
}

function UnitControls({ clip, setPub, setEmbed, onNudge, nudging }: { clip: Asset; setPub: (p: string) => void; setEmbed: (v: boolean) => void; onNudge: () => void; nudging: boolean }) {
  return (
    <>
      <span className="ctl">
        <span className="label">发布</span>
        <span className="seg" role="group" aria-label="发布级别">
          {["P1", "P2", "P3", "ARCHIVE"].map((p) => (
            <button key={p} className={clip.publication_priority === p ? "on" : ""} aria-pressed={clip.publication_priority === p} onClick={() => setPub(p)}>{p === "ARCHIVE" ? "存档" : p}</button>
          ))}
        </span>
      </span>
      <Switch on={!!clip.embed} onChange={setEmbed}>进入 Notion 正文</Switch>
      <button className="btn ghost sm" onClick={onNudge}>{nudging ? "收起微调" : "微调边界"}</button>
    </>
  );
}

/* ───────────────────────────── unit ───────────────────────────── */
function UnitSection({ n, u, section, assets, flags, facets, active, setActive, playAt, onTime, reload, onShot, sourceId, focusFlag, onSetFlag, mobile, inline }: {
  n: number; u: Unit; section: any; assets: Asset[]; flags: Flag[]; facets: any[]; active: Active; setActive: (a: Active) => void;
  playAt: (unitId: string, t: number) => boolean; onTime: (t: number | null) => void; reload: () => void; onShot: (shots: Asset[], i: number) => void;
  sourceId: string; focusFlag: number | null; onSetFlag: (f: Flag, r: string) => void; mobile: boolean; inline: boolean;
}) {
  const [nudging, setNudging] = useState(false);
  const [more, setMore] = useState(false);
  const [needles, setNeedles] = useState<Record<number, string>>({});
  const prose = useRef<HTMLDivElement>(null);
  const clip = assets.find((a) => a.kind === "clip" && a.unit_id === u.id);
  const sub = assets.find((a) => a.kind === "clip_subtitled" && a.unit_id === u.id);
  const poster = assets.find((a) => a.kind === "poster" && a.unit_id === u.id);
  const shots = assets.filter((a) => a.kind === "screenshot" && a.unit_id === u.id && !(poster && Math.abs(a.start - poster.start) < 3));
  const text: string = section?.markdown || `## ${u.title}\n\n${u.summary}`;
  const lines = text.trim().split("\n");
  const heading = cleanUnitTitle(lines[0].startsWith("#") ? lines[0].replace(/^#+\s*/, "") : u.title);
  const body = lines[0].startsWith("#") ? lines.slice(1).join("\n") : text;
  const fvals = u.facets || {};
  const shown = (facets || []).filter((f: any) => (Array.isArray(fvals[f.key]) ? fvals[f.key].length : fvals[f.key]));
  const html = useMemo(() => renderMd(body, shown.map((f: any) => f.label)), [body, shown.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const review = flags.filter(isReview);
  const notes = flags.filter((f) => !isReview(f));
  const anyOpen = review.some((f) => f.resolution === "OPEN");

  useLayoutEffect(() => {
    const root = prose.current;
    if (!root) return;
    const found: Record<number, string> = {};
    for (const f of review) {
      let em = root.querySelector<HTMLElement>(`em.quan[data-flag="${f.id}"]`);
      if (!em) {
        const cands = needleCandidates(f);
        for (const c of cands) {
          if (!root.textContent?.includes(c)) continue;
          em = wrapText(root, c, f.id);
          if (em) break;
        }
        if (!em) em = wrapBest(root, cands, f.id);
      }
      if (em) {
        // an exact match names the questioned words in the note's headline; a near match only marks the sentence
        if (!em.classList.contains("near")) found[f.id] = em.textContent || "";
        em.classList.toggle("done", f.resolution !== "OPEN");
      }
    }
    setNeedles((prev) => (JSON.stringify(prev) === JSON.stringify(found) ? prev : found));
  }, [html, flags]); // eslint-disable-line react-hooks/exhaustive-deps

  const setPub = async (p: string) => { await post(`/api/assets/${clip.id}`, { publication_priority: p }); reload(); };
  const setEmbed = async (v: boolean) => { await post(`/api/assets/${clip.id}`, { embed: v }); reload(); };
  const zp = (f: Flag, compact = false) => (
    <Zhupi key={f.id} f={f} needle={needles[f.id]} onSet={onSetFlag} playAt={playAt} focused={focusFlag === f.id} compact={compact} />
  );

  return (
    <>
      <article className={`unit${anyOpen ? " has-pi" : ""}`} id={`u-${u.id}`} data-unit={u.id} data-no={cnNo(n)}>
        <div className="unit-meta">
          <span className="no">{cnNo(n)}</span>
          <span className="num">{tc(u.start)}<span className="to">–</span>{tc(u.end)}</span>
          {u.learning_priority === 3 && <span className="tag must">必学</span>}
          {(u.tickers || []).slice(0, 5).map((t: string) => <span key={t} className="tag tk">{t}</span>)}
        </div>
        <h2 className="unit-title"><Words>{heading}</Words></h2>
        {inline && review.length > 0 && <div className="pi-cards">{review.map((f) => zp(f, true))}</div>}
        <Film unit={u} clip={clip} sub={sub} poster={poster} active={active} setActive={setActive} onTime={onTime} eager={n <= 2} />
        {shots.length > 0 && (
          <div className={`shots${shots.length === 1 ? " one" : ""}`}>
            {shots.map((s, i) => (
              <figure className="shot" key={s.id}>
                <button className="shot-btn" onClick={() => onShot(shots, i)} aria-label={`放大截图 ${tc(s.start)}`}>
                  <img src={media(s.id, 640)} srcSet={srcSet(s.id)} sizes="(max-width: 900px) 84vw, 320px" alt={s.meta?.reason || ""} loading="lazy" width={1920} height={1080} />
                </button>
                <figcaption><span className="num">{tc(s.start)}</span>{s.meta?.reason && typeset(s.meta.reason)}</figcaption>
              </figure>
            ))}
          </div>
        )}
        <div className="prose" ref={prose} dangerouslySetInnerHTML={{ __html: html }} />
        {shown.length > 0 && (
          <dl className="facets">
            {shown.map((f: any) => (
              <div className="facet" key={f.key}>
                <dt>{f.label}</dt>
                <dd>{Array.isArray(fvals[f.key]) ? <ol>{fvals[f.key].map((x: string, i: number) => <li key={i}>{typeset(x)}</li>)}</ol> : typeset(fvals[f.key])}</dd>
              </div>
            ))}
          </dl>
        )}
        {clip?.status === "GENERATED" && <div className="unit-actions">
          {mobile ? (
            <button className="iconbtn more" onClick={() => setMore(true)} aria-label="片段设置">
              <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden><circle cx="5" cy="12" r="1.6" fill="currentColor" /><circle cx="12" cy="12" r="1.6" fill="currentColor" /><circle cx="19" cy="12" r="1.6" fill="currentColor" /></svg>
            </button>
          ) : <UnitControls clip={clip} setPub={setPub} setEmbed={setEmbed} nudging={nudging} onNudge={() => setNudging(!nudging)} />}
        </div>}
        {!mobile && nudging && clip && <Nudge sourceId={sourceId} clip={clip} onDone={() => { setNudging(false); reload(); }} />}
        {mobile && more && clip && (
          <Sheet title="片段设置" onClose={() => { setMore(false); setNudging(false); }} tall={nudging}>
            <div className="sheet-ctl"><UnitControls clip={clip} setPub={setPub} setEmbed={setEmbed} nudging={nudging} onNudge={() => setNudging(!nudging)} /></div>
            {nudging && <Nudge sourceId={sourceId} clip={clip} onDone={() => { setNudging(false); setMore(false); reload(); }} />}
          </Sheet>
        )}
      </article>
      <aside className="margin" aria-label="校注">
        {!inline && review.length > 0 && <div className="margin-stack">{review.map((f) => zp(f))}</div>}
        {notes.length > 0 && (
          <details className="notes">
            <summary>{`注记 ${notes.length} 条`}</summary>
            {notes.map((f) => zp(f))}
          </details>
        )}
      </aside>
    </>
  );
}

/* ───────────────────────────── onboarding ───────────────────────────── */
function Track({ source, onDone }: { source: any; onDone: () => void }) {
  const [why, setWhy] = useState("");
  const [name, setName] = useState(source.channel_title || "");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  return (
    <div className="track">
      <h3>把「{source.channel_title}」加入长期关注</h3>
      <label>频道名<input value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label>你主要为什么看这个频道？<textarea rows={3} value={why} onChange={(e) => setWhy(e.target.value)} placeholder="例如：学习他怎么判断宏观周期，以及他引用的数据和论证过程" /></label>
      {!draft ? (
        <button className="btn solid" disabled={why.trim().length < 6 || !!busy} onClick={async () => {
          setBusy("起草中（约 1 分钟）…");
          try { const r = await post("/api/channels/draft", { name, why, source_id: source.id }); setDraft(r.raw_prompt); setBusy(null); }
          catch (e) { setBusy(String((e as Error).message)); }
        }}>起草学习目标</button>
      ) : (
        <>
          <label>频道学习目标（可直接修改）<textarea rows={16} value={draft} onChange={(e) => setDraft(e.target.value)} /></label>
          <button className="btn solid" disabled={!!busy} onClick={async () => {
            setBusy("保存并编译…");
            try { await post("/api/channels/track", { name, why, prompt: draft, source_id: source.id }); setBusy("已加入长期关注。规程编译完成后会出现在首页「待你拍板」。"); setTimeout(onDone, 2000); }
            catch (e) { setBusy(String((e as Error).message)); }
          }}>保存并编译</button>
        </>
      )}
      {busy && <p className="zhu track-msg">{busy}</p>}
    </div>
  );
}

/* ───────────────────────────── 校注 stepper ───────────────────────────── */
/** 校注 are information, not a to-do (design §R2-3): step through the ones not yet looked at. */
function Stepper({ count, total, pos, onStep }: { count: number; total: number; pos: number; onStep: (d: number) => void }) {
  if (count === 0) return <span className="rb-clear">{total ? "校注已看完" : "本期无校注"}</span>;
  return (
    <span className="stepper" role="group" aria-label="逐条查看校注">
      <button onClick={() => onStep(-1)} aria-label="上一处校注">‹</button>
      <button className="stepper-mid" onClick={() => onStep(pos < 0 ? 1 : 0)} title="快捷键 n / Shift+n">
        <span className="pi-mark" aria-hidden>校</span>
        {pos < 0 ? <><span className="num">{count}</span><span className="u">处</span></> : <><span className="num">{pos + 1}</span><span className="u">/</span><span className="num">{count}</span></>}
      </button>
      <button onClick={() => onStep(1)} aria-label="下一处校注">›</button>
    </span>
  );
}

/* ───────────────────────────── 牌记: the issue's colophon, set vertically like the end of a woodblock book ───────────────────────────── */
function Paiji({ kicker, source, units, flags, tuning }: { kicker: string; source: any; units: Unit[]; flags: Flag[]; tuning: boolean }) {
  const review = flags.filter(isReview);
  const by = (r: string) => review.filter((f) => f.resolution === r).length;
  const open = by("OPEN");
  const must = units.filter((u) => u.learning_priority === 3).length;
  const pub = parseDay(source.published_at);
  const judged = [by("ACCEPTED") && `无误${cnNum(by("ACCEPTED"))}`, by("FIXED") && `改${cnNum(by("FIXED"))}`, by("DISMISSED") && `略${cnNum(by("DISMISSED"))}`].filter(Boolean).join("、");
  return (
    <div className="paiji" role="note" aria-label="本期牌记">
      <p>课代表 · {kicker}</p>
      {pub && <p>原讲于{cnYear(pub.getFullYear())}年{cnDay(source.published_at)}</p>}
      <p>{cnNum(units.length)}单元，必学{cnNum(must)}</p>
      <p>校注{cnNum(review.length)}处{open ? `，未看${cnNum(open)}处` : judged ? `：${judged}` : ""}</p>
      <p className="paiji-last">{tuning ? "调试样片，定稿后入 Notion" : source.delivery === "DELIVERED" ? `${source.delivered_at ? cnDay(source.delivered_at) : ""}入 Notion` : "尚未入 Notion"}</p>
    </div>
  );
}

/* ───────────────────────────── page ───────────────────────────── */
function Progress() {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let raf = 0;
    const on = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const h = document.documentElement.scrollHeight - innerHeight;
        if (ref.current) ref.current.style.transform = `scaleX(${h > 0 ? scrollY / h : 0})`;
      });
    };
    on();
    addEventListener("scroll", on, { passive: true });
    addEventListener("resize", on);
    return () => { removeEventListener("scroll", on); removeEventListener("resize", on); cancelAnimationFrame(raf); };
  }, []);
  return <div className="progress" ref={ref} aria-hidden />;
}

/** No article to show: still on the press, or it failed, and then why and what can be done about it here. */
function NotPrinted({ run, reload }: { run: any; reload: () => void }) {
  const [busy, setBusy] = useState(false);
  const act = async (what: string) => {
    setBusy(true);
    try { await post(`/api/runs/${encodeURIComponent(run.id)}/${what}`); reload(); } finally { setBusy(false); }
  };
  if (run?.production !== "FAILED") return <Empty title={run?.production === "CANCELLED" ? "这一期已取消" : "这一期还在付印"} />;
  return (
    <Empty title={failText(run.error_class, run.retry)} action={
      <>
        <button className="btn" disabled={busy} onClick={() => act("ignore")}>忽略</button>
        {run.kind !== "IMPORT" && <button className="btn solid" disabled={busy} onClick={() => act("retry")}>重试</button>}
      </>
    } />
  );
}

export default function Reader({ id }: { id: string }) {
  // 频道调试台 links to one version of an issue: /issue/<id>?run=<run id>
  const runParam = new URLSearchParams(location.search).get("run");
  const { data, error, loadedAt, reload, setData } = usePoll<any>(
    `/api/sources/${encodeURIComponent(id)}${runParam ? `?run=${encodeURIComponent(runParam)}` : ""}`, 0);
  const mobile = useMedia("(max-width: 900px)");
  const [active, setActiveState] = useState<Active>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [lb, setLb] = useState<{ shots: Asset[]; i: number } | null>(null);
  const [tracking, setTracking] = useState(false);
  const [indexOpen, setIndexOpen] = useState(false);
  const [pos, setPos] = useState(-1);
  const [focusFlag, setFocusFlag] = useState<number | null>(null);
  const [playhead, setPlayhead] = useState<number | null>(null);
  const [stuck, setStuck] = useState(false);
  const inline = useMedia("(max-width: 1240px)");
  const sentinel = useRef<HTMLDivElement>(null);
  const setActive = useCallback((a: Active) => setActiveState(a), []);
  const onTime = useCallback((t: number | null) => setPlayhead(t == null ? null : Math.round(t * 2) / 2), []);
  useRestoreScroll(!!data);

  const units: Unit[] = useMemo(() => (data?.units || []).slice().sort((a: Unit, b: Unit) => a.start - b.start), [data]);
  const flags: Flag[] = data?.flags || [];
  const order = useMemo(() => Object.fromEntries(units.map((u, i) => [u.id, i])), [units]);
  // the review queue in reading order: issue-level notes first, then unit by unit
  const queue: Flag[] = useMemo(() => flags.filter(isOpen).sort((a, b) =>
    (order[a.target_id] ?? -1) - (order[b.target_id] ?? -1) || a.id - b.id), [flags, order]);

  const firstMust = units.find((u) => u.learning_priority === 3);
  const headline = data?.source ? issueHeadline(data.source.headline, firstMust?.title, data.source.title || data.source.id) : "";
  useEffect(() => {
    if (headline) document.title = `${headline} · 课代表`;
  }, [headline]);

  useEffect(() => {
    if (!units.length) return;
    const els = units.map((u) => document.getElementById(`u-${u.id}`)).filter(Boolean) as HTMLElement[];
    const io = new IntersectionObserver((entries) => {
      const vis = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (vis[0]) setCurrent((vis[0].target as HTMLElement).dataset.unit || null);
    }, { rootMargin: "-140px 0px -55% 0px" });
    els.forEach((e) => io.observe(e));
    return () => io.disconnect();
  }, [units]);

  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => setStuck(!e.isIntersecting), { rootMargin: "-80px 0px 0px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [!!data]); // eslint-disable-line react-hooks/exhaustive-deps

  /** units below the fold are lazily laid out (content-visibility) and their stills lazily loaded:
   *  wake the target first, then scroll, then correct once the real height is known */
  const land = useCallback((el: HTMLElement | null, opts: ScrollIntoViewOptions) => {
    if (!el) return;
    el.closest(".unit")?.querySelectorAll<HTMLImageElement>("img[loading=lazy]").forEach((i) => { i.loading = "eager"; });
    el.scrollIntoView({ ...opts, behavior: reduceMotion() ? "auto" : opts.behavior });
    const before = el.getBoundingClientRect().top;
    setTimeout(() => {
      const now = el.getBoundingClientRect().top;
      if (Math.abs(now - before) > 0 && Math.abs(now) > innerHeight) el.scrollIntoView({ ...opts, behavior: "auto" });
    }, 750);
  }, []);
  const jump = useCallback((uid: string, smooth = true) => {
    land(document.getElementById(`u-${uid}`), { behavior: smooth ? "smooth" : "auto" });
  }, [land]);

  // deep link from the home page: /issue/x#u-<unit>
  useEffect(() => {
    if (!units.length || !location.hash.startsWith("#u-")) return;
    const uid = location.hash.slice(3);
    requestAnimationFrame(() => jump(uid, false));
  }, [units.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const focusOn = useCallback((f: Flag) => {
    setFocusFlag(f.id);
    setTimeout(() => setFocusFlag((x) => (x === f.id ? null : x)), 1600);
    const visible = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)].find((e) => e.offsetParent !== null);
    const quan = visible(`em.quan[data-flag="${f.id}"]`);
    document.querySelectorAll(`em.quan[data-flag="${f.id}"]`).forEach((e) => {
      e.classList.add("focus");
      setTimeout(() => e.classList.remove("focus"), 1800);
    });
    // three columns: the circled sentence lands level with the sticky note; stacked layouts centre it
    const target = (inline ? visible(`.zhupi[data-flag="${f.id}"]`) : quan) || quan || visible(`.zhupi[data-flag="${f.id}"]`);
    if (target) land(target, { block: inline ? "center" : "start", behavior: "smooth" });
    else if (f.target_id) jump(f.target_id);
  }, [jump, land, inline]);

  const step = useCallback((d: number) => {
    if (!queue.length) return;
    const next = pos < 0 ? (d < 0 ? queue.length - 1 : 0) : (pos + d + queue.length) % queue.length;
    setPos(next);
    focusOn(queue[next]);
  }, [queue, pos, focusOn]);

  const onSetFlag = useCallback(async (f: Flag, r: string) => {
    setData((d: any) => d && ({ ...d, flags: d.flags.map((x: Flag) => (x.id === f.id ? { ...x, resolution: r } : x)) }));
    try { await post(`/api/flags/${f.id}`, { resolution: r }); } catch { reload(); return; }
    if (r !== "OPEN" && isReview(f)) {
      // the resolved note leaves the queue: the same position now holds the next one
      const rest = queue.filter((x) => x.id !== f.id);
      const idx = Math.max(0, queue.findIndex((x) => x.id === f.id));
      if (rest.length) {
        const k = Math.min(idx, rest.length - 1);
        setPos(k);
        setTimeout(() => focusOn(rest[k]), 700);
      } else setPos(-1);
    }
  }, [queue, setData, reload, focusOn]);

  const playAt = useCallback((unitId: string, t: number) => {
    const assets: Asset[] = data?.assets || [];
    const clip = assets.find((a) => a.kind === "clip" && a.unit_id === unitId && a.status === "GENERATED");
    if (!clip) return false;
    const sub = assets.find((a) => a.kind === "clip_subtitled" && a.unit_id === unitId && a.status !== "FAILED");
    const u = units.find((x) => x.id === unitId);
    const local = Math.max(0, Math.min(clip.end - clip.start - 1, t - clip.start - 2));
    setActiveState({ assetId: (sub || clip).id, unitId, title: u?.title || "", time: local, docked: true, clipStart: clip.start, seq: Date.now() });
    return true;
  }, [data?.assets, units]);

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input,textarea,[contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "n" || e.key === "N") { e.preventDefault(); step(e.shiftKey ? -1 : 1); return; }
      if (e.key !== "j" && e.key !== "k") return;
      const i = units.findIndex((u) => u.id === current);
      const n = units[Math.max(0, Math.min(units.length - 1, i + (e.key === "j" ? 1 : -1)))];
      if (n) jump(n.id);
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [units, current, jump, step]);

  if (!data) return <main className="page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const { source, run, assets, sections, playbook, channel } = data;
  const secByUnit: Record<string, any> = {};
  sections.forEach((s: any) => s.unit_ids?.forEach((u: string) => (secByUnit[u] = s)));
  const intro = sections.filter((s: any) => s.kind === "INTRO");
  const outro = sections.filter((s: any) => s.kind === "OUTRO").map((s: any) => ({ ...s, html: renderMd(s.markdown) })).filter((s: any) => s.html.replace(/<[^>]+>/g, "").trim());
  const runFlags = flags.filter((f: Flag) => !f.target_id || !(f.target_id in order));
  const clips = assets.filter((a: Asset) => a.kind === "clip" && a.status === "GENERATED");
  const missing = new Set<string>(units.filter((u) => !clips.some((c: Asset) => c.unit_id === u.id)).map((u) => u.id));
  const duration = source.duration || units[units.length - 1]?.end || 1;
  const tuning = channel?.stage === "TUNING";
  const kicker = `${channel?.name || source.channel_title || "未识别频道"} · 第${cnNum(data.no || 1)}期`;
  const mustPoster = firstMust && assets.find((a: Asset) => a.kind === "poster" && a.unit_id === firstMust.id);
  const ci = Math.max(0, units.findIndex((u) => u.id === current));
  const orig = originalTitle(source.title || source.id);
  const must = units.filter((u) => u.learning_priority === 3).length;

  return (
    <main className="page reader-page">
      <Progress />
      <Stale error={error} loadedAt={loadedAt} />
      <header className="reader-head">
        <div className={`head-in${!inline && mustPoster ? " has-fig" : ""}`}>
        <a className="crumb" {...linkProps(runParam ? `/channels/${channel?.id || ""}?sample=${encodeURIComponent(source.id)}&run=${encodeURIComponent(runParam)}` : "/")}><span aria-hidden>←</span> {runParam ? "频道" : "目录"}</a>
        {runParam && data.run && data.run.id !== source.current_run_id && (
          <p className="version-note">正在看试跑版本{playbook ? ` · 规程 v${playbook.version}` : ""}，不是这一期当前采用的版本。</p>
        )}
        <div className="kicker">{kicker}</div>
        <h1><Words>{headline}</Words></h1>
        {orig !== headline && <p className="dek"><Words>{orig}</Words></p>}
        <div className="reader-meta">
          <span>{cnDay(source.published_at)}</span>
          <span><span className="num">{Math.round((source.duration || 0) / 60)}</span> 分钟</span>
          <span><span className="num">{units.length}</span> 单元（必学 <span className="num">{must}</span>）</span>
          <span><span className="num">{clips.length}</span> 段片段</span>
        </div>
        <div className="reader-links">
          {source.url && <a href={source.url} target="_blank" rel="noreferrer">原视频 ↗</a>}
          {playbook && <a {...linkProps(playbook.channel_id ? `/channels/${playbook.channel_id}?view=rules&pb=${playbook.id}` : "/channels/universal")}>规程 v{playbook.version}{playbook.channel_id ? "" : "（通用）"}</a>}
          {(!channel || channel.mode !== "TRACKED") && source.youtube_channel_id && (
            <button className="btn sm zhu" onClick={() => setTracking(!tracking)}>加入长期关注</button>
          )}
        </div>
        {!inline && mustPoster ? (
          <div className="head-fig">
            <a href={`#u-${firstMust.id}`} onClick={(e) => { e.preventDefault(); jump(firstMust.id); }} aria-label={`必学：${firstMust.title}`}>
              <span className="head-fig-img"><img src={media(mustPoster.id, 640)} alt="" /></span>
              {firstMust.title !== headline && <span className="head-fig-cap"><Words>{firstMust.title}</Words></span>}
            </a>
          </div>
        ) : null}
        {tracking && <Track source={source} onDone={() => { setTracking(false); reload(); }} />}
        </div>
      </header>

      <div ref={sentinel} className="rb-sentinel" aria-hidden />
      <div className={`reviewbar${stuck ? " stuck" : ""}`}>
        <div className="rb-inner">
          {mobile ? (
            <button className="rb-count" onClick={() => setIndexOpen(true)} aria-label="打开单元目录">
              <span><span className="num">{String(ci + 1).padStart(2, "0")}</span><em className="num">/{units.length}</em></span>
            </button>
          ) : tuning ? <span className="rb-state">样片</span> : null}
          <Stepper count={queue.length} total={flags.filter(isReview).length} pos={queue.length ? Math.min(pos, queue.length - 1) : -1} onStep={step} />
          {!mobile && source.delivery !== "DELIVERED" && (
            <span className="rb-deliv" title={source.delivery_reason || ""}>
              {deliveryLabel(source.delivery, source.delivery_reason)}
              {["FAILED", "STALE"].includes(source.delivery) && (
                <button className="link" onClick={async () => { await post(`/api/sources/${source.id}/deliver`); reload(); }}>重新交付</button>
              )}
            </span>
          )}
          <div className="rb-acts">
            {source.notion_url && <a className="btn sm" href={source.notion_url} target="_blank" rel="noreferrer">Notion ↗</a>}
          </div>
        </div>
        {mobile && units.length > 0 && <Shukou duration={duration} units={units} flags={flags} current={current} onJump={jump} />}
      </div>

      {indexOpen && (
        <Sheet title={<>本期 <span className="num">{units.length}</span> 个知识单元</>} tall onClose={() => setIndexOpen(false)}>
          <IssueIndex units={units} flags={flags} current={current} mobile={false} full onJump={(uid) => { setIndexOpen(false); setTimeout(() => jump(uid, false), 60); }} />
        </Sheet>
      )}

      {!run || !units.length ? (
        <NotPrinted run={run} reload={reload} />
      ) : (
        <div className="reader">
          {!mobile && (
            <div className="spine-col">
              <Spine title={kicker} duration={duration} units={units} flags={flags} missing={missing} current={current} playhead={playhead} onJump={jump} />
            </div>
          )}
          <div className="article lead">
            {intro.map((s: any) => <div key={s.id} className="prose lede" dangerouslySetInnerHTML={{ __html: renderMd(s.markdown) }} />)}
            <IssueIndex units={units} flags={flags} current={current} onJump={jump} mobile={mobile} />
          </div>
          <aside className="margin" aria-label="本期校注">
            {runFlags.filter(isReview).map((f: Flag) => (
              <Zhupi key={f.id} f={f} onSet={onSetFlag} playAt={playAt} focused={focusFlag === f.id} />
            ))}
            {runFlags.some((f: Flag) => !isReview(f)) && (
              <details className="notes">
                <summary>{`注记 ${runFlags.filter((f: Flag) => !isReview(f)).length} 条`}</summary>
                {runFlags.filter((f: Flag) => !isReview(f)).map((f: Flag) => <Zhupi key={f.id} f={f} onSet={onSetFlag} playAt={playAt} focused={false} />)}
              </details>
            )}
          </aside>
          {units.map((u, i) => (
            <UnitSection key={u.id} n={i + 1} sourceId={source.id} u={u} section={secByUnit[u.id]} assets={assets} facets={playbook?.facets}
              flags={flags.filter((f: Flag) => f.target_id === u.id)} active={active} setActive={setActive} playAt={playAt} onTime={onTime}
              reload={reload} onShot={(shots, k) => setLb({ shots, i: k })} focusFlag={focusFlag} onSetFlag={onSetFlag} mobile={mobile} inline={inline} />
          ))}
          {outro.map((s: any) => (
            <div key={s.id} className="article unit outro"><div className="prose" dangerouslySetInnerHTML={{ __html: s.html }} /></div>
          ))}
          <section className="article end" aria-label="阅毕">
            <div className="end-mark" aria-hidden><span>阅</span><span>毕</span></div>
            <Paiji kicker={kicker} source={source} units={units} flags={flags} tuning={tuning} />
            {queue.length > 0 && <div className="end-row"><button className="btn" onClick={() => step(1)}>逐条看校注</button></div>}
          </section>
        </div>
      )}

      <Dock active={active} setActive={setActive} onTime={onTime} onBack={() => { if (active) { jump(active.unitId); setActive({ ...active, docked: false, seq: Date.now() }); } }} />
      {lb && <Lightbox shots={lb.shots} index={lb.i} onClose={() => setLb(null)} />}
    </main>
  );
}
