import DOMPurify from "dompurify";
import { marked } from "marked";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { media, post } from "../api";
import { usePoll } from "../hooks";
import { linkProps } from "../router";
import { DELIVERY, pubDate, REVIEW, tc } from "../util";

marked.setOptions({ gfm: true, breaks: false });
const md = (s: string) => ({ __html: DOMPurify.sanitize(marked.parse(s || "") as string) });

type Unit = any;
type Asset = any;
type Flag = any;
type Active = { assetId: number; unitId: string; title: string; time: number; docked: boolean } | null;

const FLAG_KIND: Record<string, string> = {
  BOUNDARY_FALLBACK: "边界兜底", BOUNDARY_UNCERTAIN: "边界待确认", CONTEXT_MISSING: "缺少上下文",
  CLIP_TOO_SHORT: "片段过短", MEDIA_QA_FAILED: "媒体质检", UNIT_DETAIL_MISSING: "内容缺失", SECTION_MISSING: "正文缺失",
  EDITORIAL_TERM: "措辞", MERGE_CONFLICT: "分段矛盾", OVERSTATEMENT_CANDIDATE: "程度", CLAIM_MISMATCH: "陈述核对",
  ATTRIBUTION_OR_CONDITION_REVIEW_REQUIRED: "条件与归属", CLIP_CONTEXT_REVIEW_REQUIRED: "上下文依赖",
  PROVIDER_OUTPUT_LIMIT: "模型输出上限", PARSER_FAILURE: "解析恢复", STRUCTURED_CONTRACT_FAILURE: "结构化失败",
  ORCHESTRATION_FAILURE: "编排", SOURCE_TIMESTAMP_OVERRUN: "时间越界", EDITORIAL_REVIEW_CANDIDATE: "措辞",
  ANCHOR_OUT_OF_RANGE: "截图锚点", HALF_SENTENCE_START: "片段起点", NEXT_TOPIC_BLEED: "片段终点", ENDS_BEFORE_CONCLUSION: "片段终点",
  DANGLING_REFERENCE: "指代", ATTRIBUTION_RISK: "归属", OVERSTATEMENT: "程度", CLIP_SHARED: "共用片段", UNIT_SPLIT_SPAN: "单元区间",
  CLIP_TOO_LONG: "片段过长", SELECTION_FALLBACK: "选片兜底", MERGE_UNREFERENCED_TOPIC: "合并", SCREENSHOT_FAILED: "截图",
  BAD_TIMECODE: "时间码", CLIP_RENDER_FAILED: "剪辑失败",
};
const flagKind = (k: string) => FLAG_KIND[k] || FLAG_KIND[k.replace(/^AI_/, "")] || k.replace(/^AI_/, "").toLowerCase();

/* ───────────────────────────── spine ───────────────────────────── */
function Spine({ duration, units, assets, flags, active, onJump }: {
  duration: number; units: Unit[]; assets: Asset[]; flags: Flag[]; active: string | null; onJump: (id: string) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [h, setH] = useState(600);
  const [hover, setHover] = useState<{ u: Unit; y: number } | null>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setH(el.clientHeight));
    ro.observe(el);
    setH(el.clientHeight);
    return () => ro.disconnect();
  }, []);
  const y = (t: number) => (Math.min(duration, Math.max(0, t)) / duration) * h;
  const clips = assets.filter((a) => a.kind === "clip" && a.status === "GENERATED");
  const shots = assets.filter((a) => a.kind === "screenshot");
  const openFlags = flags.filter((f) => f.resolution === "OPEN" && f.severity !== "INFO" && f.target_id);
  const unitOf = useMemo(() => Object.fromEntries(units.map((u) => [u.id, u])), [units]);
  const step = duration > 5400 ? 1800 : duration > 2400 ? 900 : 300;
  const ticks = [];
  for (let t = 0; t <= duration; t += 300) ticks.push(t);
  const act = active ? unitOf[active] : null;
  const shotFor = (u: Unit) => shots.find((s) => s.unit_id === u.id) || assets.find((a) => a.kind === "poster" && a.unit_id === u.id);

  return (
    <div className="spine" ref={box} onMouseLeave={() => setHover(null)} aria-label="整期时间轴">
      <svg role="img" aria-label={`时长 ${tc(duration)}，${units.length} 个知识单元`}>
        <line x1="30" x2="30" y1="0" y2={h} stroke="var(--rule-2)" strokeWidth="1" />
        {ticks.map((t) => (
          <g key={t}>
            <line x1={t % step === 0 ? 24 : 27} x2="30" y1={y(t)} y2={y(t)} stroke="var(--ink-3)" strokeWidth="1" />
            {t % step === 0 && (
              <text x="20" y={y(t) + 3.5} textAnchor="end" fontSize="9.5" fill="var(--ink-3)" fontFamily="var(--mono)">
                {Math.round(t / 60)}′
              </text>
            )}
          </g>
        ))}
        {units.map((u) => {
          const y0 = y(u.start), y1 = y(u.end);
          const on = u.id === active;
          const op = u.learning_priority === 3 ? 0.9 : u.learning_priority === 2 ? 0.48 : 0.22;
          return (
            <g key={u.id} style={{ cursor: "pointer" }} onClick={() => onJump(u.id)}
              onMouseEnter={() => setHover({ u, y: (y0 + y1) / 2 })}>
              <rect x="0" y={y0} width="76" height={Math.max(4, y1 - y0)} fill="transparent" />
              <rect x="33" y={y0 + 1} width="7" height={Math.max(2, y1 - y0 - 2)} rx="1.5"
                fill={on ? "var(--zhu)" : "var(--ink)"} opacity={on ? 1 : op} style={{ transition: "fill .3s, opacity .3s" }} />
            </g>
          );
        })}
        {clips.map((c) => (
          <rect key={c.id} x="43" y={y(c.start)} width="2" height={Math.max(2, y(c.end) - y(c.start))} fill="var(--ink-2)" pointerEvents="none" />
        ))}
        {shots.map((s) => <circle key={s.id} cx="50" cy={y(s.start)} r="1.6" fill="var(--ink-3)" pointerEvents="none" />)}
        {openFlags.map((f) => {
          const u = unitOf[f.target_id];
          if (!u) return null;
          return <circle key={f.id} cx="58" cy={y(u.start) + 4} r="3.4" fill="var(--zhu)" stroke="var(--paper)" strokeWidth="1.5" pointerEvents="none" />;
        })}
        {act && (
          <g pointerEvents="none" style={{ transition: "transform .5s var(--ease)" }} transform={`translate(0 ${y(act.start)})`}>
            <line x1="26" x2="66" y1="0" y2="0" stroke="var(--zhu)" strokeWidth="1.5" />
            <text x="68" y="3.5" fontSize="9.5" fill="var(--zhu)" fontFamily="var(--mono)">{tc(act.start)}</text>
          </g>
        )}
      </svg>
      {hover && (
        <div className="tip" style={{ top: hover.y }}>
          <span className="mono">{tc(hover.u.start)} – {tc(hover.u.end)}{hover.u.learning_priority === 3 ? " · 必学" : ""}</span>
          {hover.u.title}
          {shotFor(hover.u) && <img src={media(shotFor(hover.u).id)} alt="" />}
        </div>
      )}
    </div>
  );
}

/* ───────────────────────────── video ───────────────────────────── */
function Film({ unit, clip, sub, posterId, active, setActive }: {
  unit: Unit; clip?: Asset; sub?: Asset; posterId?: number; active: Active; setActive: (a: Active) => void;
}) {
  const vid = sub && sub.status !== "FAILED" ? sub : clip;
  const ref = useRef<HTMLDivElement>(null);
  const vref = useRef<HTMLVideoElement>(null);
  const isActive = !!active && !!vid && active.assetId === vid.id;
  useEffect(() => {
    if (!isActive || !ref.current) return;
    const io = new IntersectionObserver(([e]) => {
      const v = vref.current;
      if (!e.isIntersecting && v && !v.paused) {
        setActive({ assetId: vid.id, unitId: unit.id, title: unit.title, time: v.currentTime, docked: true });
      } else if (e.isIntersecting && active?.docked) {
        setActive({ ...active, docked: false });
      }
    }, { threshold: 0.25 });
    io.observe(ref.current);
    return () => io.disconnect();
  }, [isActive, active, setActive, unit, vid]);
  useEffect(() => {
    if (isActive && !active!.docked && vref.current) {
      vref.current.currentTime = active!.time || 0;
      vref.current.play().catch(() => {});
    }
  }, [isActive, active?.docked]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!clip) return null;
  if (clip.status === "NOT_SELECTED" && clip.meta?.shared_with) {
    return <div className="film-missing">这一段与「{clip.meta.shared_title}」落在同一段视频里，片段见该单元。</div>;
  }
  if (clip.status !== "GENERATED") {
    return <div className="film-missing">这一段的视频片段没有生成：{clip.meta?.error || "原因见朱批"}。文字与截图照常保留。</div>;
  }
  return (
    <div className="film" ref={ref}>
      {isActive && !active!.docked ? (
        <video ref={vref} src={media(vid.id)} controls autoPlay playsInline preload="auto" />
      ) : (
        <>
          <img src={media(posterId)} alt="" loading="lazy"
            onError={(e) => { (e.target as HTMLImageElement).style.visibility = "hidden"; }} />
          <button className="play" onClick={() => setActive({ assetId: vid.id, unitId: unit.id, title: unit.title, time: 0, docked: false })}
            aria-label={`播放：${unit.title}`}>
            <span className="disc">
              {isActive ? <span style={{ font: "500 11px var(--sans)" }}>小窗中</span> :
                <svg width="20" height="20" viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5-11-6.5Z" fill="currentColor" /></svg>}
            </span>
          </button>
          <div className="cap">
            <span>{tc(clip.start)} – {tc(clip.end)}</span>
            <span className="tag">{Math.round(clip.end - clip.start)} 秒</span>
            {sub && sub.status !== "FAILED" && <span className="tag">字幕</span>}
            {!clip.standalone && <span className="tag">依赖前文</span>}
          </div>
        </>
      )}
    </div>
  );
}

function Dock({ active, setActive, onBack }: { active: Active; setActive: (a: Active) => void; onBack: () => void }) {
  const vref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (vref.current && active) { vref.current.currentTime = active.time; vref.current.play().catch(() => {}); }
  }, [active?.assetId, active?.docked]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!active?.docked) return null;
  return (
    <div className="dock" role="region" aria-label="小窗播放">
      <video ref={vref} src={media(active.assetId)} controls playsInline
        onTimeUpdate={(e) => { active.time = (e.target as HTMLVideoElement).currentTime; }} />
      <div className="bar">
        <span>{active.title}</span>
        <button onClick={onBack}>回到正文</button>
        <button onClick={() => setActive(null)} aria-label="关闭">✕</button>
      </div>
    </div>
  );
}

/* ───────────────────────────── margin ───────────────────────────── */
function Zhupi({ f, onChange }: { f: Flag; onChange: () => void }) {
  const set = async (resolution: string) => { await post(`/api/flags/${f.id}`, { resolution }); onChange(); };
  const resolved = f.resolution !== "OPEN";
  const quote = f.evidence?.quote || f.evidence?.source_range;
  return (
    <div className={`zhupi ${resolved ? "resolved" : ""} ${f.severity === "INFO" ? "info" : ""}`}>
      <div className="k">{flagKind(f.kind)}{f.origin === "AI_CHECK" ? " · 预审" : ""}</div>
      <h5>{f.title}</h5>
      {f.body && <p>{f.body}</p>}
      {quote && <blockquote>{quote}</blockquote>}
      <div className="acts">
        {resolved ? (
          <>
            <span className="muted" style={{ fontSize: 12.5, alignSelf: "center" }}>
              {f.resolution === "ACCEPTED" ? "已确认无误" : f.resolution === "FIXED" ? "已修正" : "已忽略"}
            </span>
            <button className="btn ghost sm" onClick={() => set("OPEN")}>撤回</button>
          </>
        ) : f.severity === "INFO" ? (
          <button className="btn ghost sm" onClick={() => set("DISMISSED")}>知道了</button>
        ) : (
          <>
            <button className="btn sm" onClick={() => set("ACCEPTED")}>确认无误</button>
            <button className="btn ghost sm" onClick={() => set("FIXED")}>已修正</button>
            <button className="btn ghost sm" onClick={() => set("DISMISSED")}>忽略</button>
          </>
        )}
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
    Promise.all([q(clip.start - 8, clip.start + 6), q(clip.end - 6, clip.end + 8)]).then(([h, t]) => setWords({ head: h.words, tail: t.words }));
  }, [sourceId, clip.start, clip.end]);
  const submit = async () => {
    try { await post(`/api/assets/${clip.id}/nudge`, { start, end }); setState("已提交，机房空闲时重剪（约 1 分钟）"); setTimeout(onDone, 1500); }
    catch (e) { setState(String((e as Error).message)); }
  };
  const strip = (ws: any[], kind: "start" | "end") => (
    <div className="strip">
      {ws.length === 0 && <span className="muted">（这一期没有全片转写，只能按秒微调）</span>}
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
        <span className="caps">起点</span>
        <span className="mono">{tc(start)}</span>
        <div className="step"><button onClick={() => setStart(start - 1)}>−1s</button><button onClick={() => setStart(start + 1)}>+1s</button></div>
      </div>
      {words && strip(words.head, "start")}
      <div className="nudge-row">
        <span className="caps">终点</span>
        <span className="mono">{tc(end)}</span>
        <div className="step"><button onClick={() => setEnd(end - 1)}>−1s</button><button onClick={() => setEnd(end + 1)}>+1s</button></div>
      </div>
      {words && strip(words.tail, "end")}
      <div className="nudge-row" style={{ marginTop: 12 }}>
        <span className="muted" style={{ fontSize: 12.5 }}>点选词语把边界吸附到词边；确定性重剪，不调用模型。时长 {Math.round(end - start)} 秒。</span>
        <button className="btn solid sm" style={{ marginLeft: "auto" }} onClick={submit} disabled={end - start < 3}>按此重剪</button>
      </div>
      {state && <p className="zhu" style={{ margin: "8px 0 0", fontSize: 13 }}>{state}</p>}
    </div>
  );
}

/* ───────────────────────────── unit ───────────────────────────── */
function UnitSection({ u, section, assets, flags, facets, active, setActive, reload, onShot, sourceId }: any) {
  const [nudging, setNudging] = useState(false);
  const clip = assets.find((a: Asset) => a.kind === "clip" && a.unit_id === u.id);
  const sub = assets.find((a: Asset) => a.kind === "clip_subtitled" && a.unit_id === u.id);
  const poster = assets.find((a: Asset) => a.kind === "poster" && a.unit_id === u.id);
  const shots = assets.filter((a: Asset) => a.kind === "screenshot" && a.unit_id === u.id);
  const text: string = section?.markdown || `## ${u.title}\n\n${u.summary}`;
  const lines = text.trim().split("\n");
  const heading = lines[0].startsWith("#") ? lines[0].replace(/^#+\s*/, "") : u.title;
  const body = lines[0].startsWith("#") ? lines.slice(1).join("\n") : text;
  const fdefs: any[] = facets?.length ? facets : [];
  const fvals = u.facets || {};
  const shown = fdefs.filter((f) => (Array.isArray(fvals[f.key]) ? fvals[f.key].length : fvals[f.key]));
  const setDecision = async (d: string | null) => { await post(`/api/units/${u.id}`, { decision: d }); reload(); };
  const setPub = async (p: string) => { await post(`/api/assets/${clip.id}`, { publication_priority: p }); reload(); };
  const setEmbed = async (v: boolean) => { await post(`/api/assets/${clip.id}`, { embed: v }); reload(); };

  return (
    <>
      <article className={`unit ${u.decision === "REJECTED" ? "rejected" : ""}`} id={`u-${u.id}`} data-unit={u.id}>
        <div className="unit-meta">
          <span className="tc">{tc(u.start)} – {tc(u.end)}</span>
          {u.learning_priority === 3 && <span className="must">必学</span>}
          {u.learning_priority !== 3 && <span className="stars" aria-label={`学习优先级 ${u.learning_priority}`}>{"★".repeat(u.learning_priority)}</span>}
          {(u.tickers || []).slice(0, 6).map((t: string) => <span key={t} className="ticker">{t}</span>)}
        </div>
        <h2 className="unit-title">{heading}</h2>
        <Film unit={u} clip={clip} sub={sub} posterId={poster?.id} active={active} setActive={setActive} />
        {shots.length > 0 && (
          <div className="shots">
            {shots.map((s: Asset) => (
              <figure className="shot" key={s.id} onClick={() => onShot(s)}>
                <img src={media(s.id)} alt={s.meta?.reason || ""} loading="lazy" />
                <figcaption><span className="mono">{tc(s.start)}</span>{s.meta?.reason}</figcaption>
              </figure>
            ))}
          </div>
        )}
        <div className="prose" dangerouslySetInnerHTML={md(body)} />
        {shown.length > 0 && (
          <dl className="facets">
            {shown.map((f) => (
              <div className="facet" key={f.key}>
                <dt>{f.label}</dt>
                <dd>{Array.isArray(fvals[f.key]) ? <ul>{fvals[f.key].map((x: string, i: number) => <li key={i}>{x}</li>)}</ul> : fvals[f.key]}</dd>
              </div>
            ))}
          </dl>
        )}
        <div className="unit-actions">
          <div className="seg" role="group" aria-label="采用">
            <button className={u.decision === "APPROVED" ? "on" : ""} onClick={() => setDecision(u.decision === "APPROVED" ? null : "APPROVED")}>采用</button>
            <button className={u.decision === "REJECTED" ? "on" : ""} onClick={() => setDecision(u.decision === "REJECTED" ? null : "REJECTED")}>不采用</button>
          </div>
          {clip?.status === "GENERATED" && (
            <>
              <span>发布</span>
              <div className="seg" role="group" aria-label="发布优先级">
                {["P1", "P2", "P3", "ARCHIVE"].map((p) => (
                  <button key={p} className={clip.publication_priority === p ? "on" : ""} onClick={() => setPub(p)}>{p === "ARCHIVE" ? "存档" : p}</button>
                ))}
              </div>
              <label className={`switch ${clip.embed ? "on" : ""}`} onClick={() => setEmbed(!clip.embed)}>
                <i /> 进入 Notion 正文
              </label>
              <button className="btn ghost sm" onClick={() => setNudging(!nudging)}>{nudging ? "收起微调" : "微调边界"}</button>
            </>
          )}
        </div>
        {nudging && clip && <Nudge sourceId={sourceId} clip={clip} onDone={() => { setNudging(false); reload(); }} />}
      </article>
      <aside className="margin">
        {flags.filter((f: Flag) => f.severity !== "INFO").map((f: Flag) => <Zhupi key={f.id} f={f} onChange={reload} />)}
        {flags.some((f: Flag) => f.severity === "INFO") && (
          <details className="notes">
            <summary>注记 {flags.filter((f: Flag) => f.severity === "INFO").length} 条</summary>
            {flags.filter((f: Flag) => f.severity === "INFO").map((f: Flag) => <Zhupi key={f.id} f={f} onChange={reload} />)}
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
      <p className="muted">以后这个频道的视频会按你的学习目标生产。先告诉系统你为什么看它，系统起草一份频道学习 Prompt，你改完再编译。</p>
      <label>频道名<input value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label>你主要为什么看这个频道？<textarea rows={3} value={why} onChange={(e) => setWhy(e.target.value)} placeholder="例如：学习他怎么判断宏观周期，以及他引用的数据和论证过程" /></label>
      {!draft ? (
        <button className="btn solid sm" disabled={why.trim().length < 6 || !!busy} onClick={async () => {
          setBusy("起草中（约 1 分钟）…");
          try { const r = await post("/api/channels/draft", { name, why, source_id: source.id }); setDraft(r.raw_prompt); setBusy(null); }
          catch (e) { setBusy(String((e as Error).message)); }
        }}>起草学习 Prompt</button>
      ) : (
        <>
          <label>频道学习 Prompt（可直接修改）<textarea rows={16} value={draft} onChange={(e) => setDraft(e.target.value)} /></label>
          <button className="btn solid sm" disabled={!!busy} onClick={async () => {
            setBusy("保存并编译…");
            try { await post("/api/channels/track", { name, why, prompt: draft, source_id: source.id }); setBusy("已加入长期关注，Playbook 编译完成后会出现在「待你拍板」。"); setTimeout(onDone, 2000); }
            catch (e) { setBusy(String((e as Error).message)); }
          }}>保存并编译</button>
        </>
      )}
      {busy && <p className="zhu" style={{ fontSize: 13 }}>{busy}</p>}
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
        if (ref.current) ref.current.style.width = `${h > 0 ? (scrollY / h) * 100 : 0}%`;
      });
    };
    on();
    addEventListener("scroll", on, { passive: true });
    addEventListener("resize", on);
    return () => { removeEventListener("scroll", on); removeEventListener("resize", on); cancelAnimationFrame(raf); };
  }, []);
  return <div className="progress" ref={ref} aria-hidden />;
}

export default function Reader({ id }: { id: string }) {
  const { data, reload } = usePoll<any>(`/api/sources/${encodeURIComponent(id)}`, 0);
  const [active, setActiveState] = useState<Active>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [shot, setShot] = useState<Asset | null>(null);
  const [tracking, setTracking] = useState(false);
  const setActive = useCallback((a: Active) => setActiveState(a), []);

  const units: Unit[] = useMemo(() => (data?.units || []).slice().sort((a: Unit, b: Unit) => a.start - b.start), [data]);

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

  const jump = useCallback((uid: string) => {
    document.getElementById(`u-${uid}`)?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, []);

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input,textarea")) return;
      if (e.key === "Escape") setShot(null);
      if (e.key !== "j" && e.key !== "k") return;
      const i = units.findIndex((u) => u.id === current);
      const n = units[Math.max(0, Math.min(units.length - 1, i + (e.key === "j" ? 1 : -1)))];
      if (n) jump(n.id);
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [units, current, jump]);

  if (!data) return <div className="loading">展卷</div>;
  const { source, run, assets, flags, sections, playbook, channel } = data;
  const secByUnit: Record<string, any> = {};
  sections.forEach((s: any) => s.unit_ids?.forEach((u: string) => (secByUnit[u] = s)));
  const intro = sections.filter((s: any) => s.kind === "INTRO");
  const outro = sections.filter((s: any) => s.kind === "OUTRO");
  const runFlags = flags.filter((f: Flag) => !f.target_id || !units.some((u) => u.id === f.target_id));
  const openFlags = flags.filter((f: Flag) => f.resolution === "OPEN" && f.severity !== "INFO").length;
  const setReview = async (r: string) => { await post(`/api/sources/${source.id}/review`, { review: r }); reload(); };
  const clips = assets.filter((a: Asset) => a.kind === "clip" && a.status === "GENERATED").length;
  const firstOpen = flags.find((f: Flag) => f.resolution === "OPEN" && f.severity !== "INFO" && f.target_id);

  return (
    <main className="page">
      <Progress />
      <header className="reader-head">
        <a className="crumb" {...linkProps("/")}>← 目录</a>
        <div className="chan">{channel?.name || source.channel_title || "未识别频道"}</div>
        <h1>{source.title || source.id}</h1>
        <div className="reader-meta">
          <span>{pubDate(source.published_at)}</span>
          <span>{tc(source.duration)}</span>
          <span>{units.length} 个知识单元 · {clips} 段片段</span>
          {playbook && <span>Playbook v{playbook.version}{playbook.channel_id ? "" : "（通用）"}</span>}
          {source.url && <a href={source.url} target="_blank" rel="noreferrer">原视频 ↗</a>}
          {source.notion_url && <a href={source.notion_url} target="_blank" rel="noreferrer">Notion ↗</a>}
          {(!channel || channel.mode !== "TRACKED") && source.youtube_channel_id && (
            <button className="btn sm zhu" onClick={() => setTracking(!tracking)}>加入长期关注</button>
          )}
        </div>
        {tracking && <Track source={source} onDone={() => { setTracking(false); reload(); }} />}
      </header>

      <div className="reviewbar">
        <span className="state">{REVIEW[source.review]}</span>
        {openFlags > 0 ? (
          <button className="flagsum" onClick={() => firstOpen && jump(firstOpen.target_id)}>朱批 {openFlags} 处待判断 →</button>
        ) : <span className="muted" style={{ fontSize: 13 }}>无待判断事项</span>}
        <span className="muted" style={{ fontSize: 12.5 }} title={source.delivery_reason || ""}>· {DELIVERY[source.delivery] || source.delivery}</span>
        {["FAILED", "STALE"].includes(source.delivery) && (
          <button className="btn ghost sm" onClick={async () => { await post(`/api/sources/${source.id}/deliver`); reload(); }}>重新交付</button>
        )}
        <div className="right">
          <button className={`btn sm ${source.review === "APPROVED" ? "solid" : ""}`} onClick={() => setReview("APPROVED")}>通过</button>
          <button className={`btn sm ${source.review === "CHANGES_REQUESTED" ? "solid" : ""}`} onClick={() => setReview("CHANGES_REQUESTED")}>需修改</button>
          <button className={`btn sm ghost ${source.review === "REJECTED" ? "zhu" : ""}`} onClick={() => setReview("REJECTED")}>驳回</button>
        </div>
      </div>

      {!run || !units.length ? (
        <div className="empty"><h3>这一期还在付印</h3><p>完成后会自动出现在这里。可以在「机房」查看进度。</p></div>
      ) : (
        <div className="reader">
          <div style={{ gridRow: "1 / span 999" }}>
            <Spine duration={source.duration || units[units.length - 1].end} units={units} assets={assets} flags={flags} active={current} onJump={jump} />
          </div>
          <div className="article">
            {intro.map((s: any) => <div key={s.id} className="prose" dangerouslySetInnerHTML={md(s.markdown)} />)}
          </div>
          <aside className="margin">
            {runFlags.filter((f: Flag) => f.severity !== "INFO").map((f: Flag) => <Zhupi key={f.id} f={f} onChange={reload} />)}
            <p className="margin-note">朱批出现在这一栏：系统对条件、归属、边界没有把握的地方，会在对应段落旁标出。</p>
            {runFlags.some((f: Flag) => f.severity === "INFO") && (
              <details className="notes">
                <summary>生产注记 {runFlags.filter((f: Flag) => f.severity === "INFO").length} 条（不需要你处理）</summary>
                {runFlags.filter((f: Flag) => f.severity === "INFO").map((f: Flag) => <Zhupi key={f.id} f={f} onChange={reload} />)}
              </details>
            )}
          </aside>
          {units.map((u) => (
            <UnitSection key={u.id} sourceId={source.id} u={u} section={secByUnit[u.id]} assets={assets} facets={playbook?.facets}
              flags={flags.filter((f: Flag) => f.target_id === u.id)} active={active} setActive={setActive} reload={reload} onShot={setShot} />
          ))}
          {outro.map((s: any) => (
            <div key={s.id} className="article unit"><div className="prose" dangerouslySetInnerHTML={md(s.markdown)} /></div>
          ))}
        </div>
      )}

      <Dock active={active} setActive={setActive} onBack={() => active && jump(active.unitId)} />
      {shot && (
        <div className="lightbox" onClick={() => setShot(null)} role="dialog" aria-label="截图">
          <div>
            <img src={media(shot.id)} alt="" />
            <p><span className="mono" style={{ opacity: 0.6, marginRight: 10 }}>{tc(shot.start)}</span>{shot.meta?.reason}</p>
          </div>
        </div>
      )}
    </main>
  );
}
