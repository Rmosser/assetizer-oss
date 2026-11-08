/* A topic: its question, the answer put together from the videos read (by sub-question, each fact citing the video and the
   moment it comes from), and the batch — each video, what it has for the question, where its read stands. A citation opens
   its video at the moment cited (pages/TopicVideo). */
import { useEffect, useMemo, useRef, useState } from "react";
import { post } from "../api";
import { Words } from "../cjk";
import { Armed, type Held, heldText, hours, itemState, type Item, len, Phases, share, Thumb, thumbOf, useToast, when } from "../components/topic/bits";
import { type Cite, citeOf, renderCited, uncited } from "../components/topic/cite";
import { type Budget, Picker } from "../components/topic/Picker";
import { Sheet, type SheetHandle } from "../components/Sheet";
import { Fault, Stale, Unroll } from "../components/States";
import { useMedia, usePoll } from "../hooks";
import { linkProps, navigate, useRestoreScroll } from "../router";
import { errorLabel } from "../util";
import "../topic.css";

type Synth = { answer: string; first: { v: number; why: string }[]; sections: { title: string; body: string }[]; videos: { v: number; adds: string }[] };
type View = {
  topic: { id: number; question: string; notes: string | null; rev: number; paused: boolean; created_at: string; updated_at: string };
  items: Item[]; counts: Record<string, number>; synth: Synth | null; synth_state: string | null; synth_error: string | null;
  synth_at: string | null; synth_n: number; synth_stale: boolean; held: Held; cost_left: number; budget: Budget;
};

const NARROW = "(max-width: 900px)";

/** Where a citation of the answer goes: its video's page, at the moment, the frame or the look it cites */
function citeHref(tid: number, it: Item | undefined, c: Cite): string | undefined {
  if (!it) return undefined;
  const q = c.look ? `?look=${encodeURIComponent(c.look)}` : c.t != null ? `?t=${c.t}${c.kind === "frame" ? "&k=frame" : ""}` : "";
  return `/topics/${tid}/${it.id}${q}`;
}

/** The answer: what the videos read say, by sub-question; what to watch first; what each video adds */
function Answer({ v, byNo, onSynth, synthing }: { v: View; byNo: Map<number, Item>; onSynth: () => void; synthing: boolean }) {
  const tid = v.topic.id;
  const s = v.synth!;
  const ways = useMemo(() => ({
    name: (c: Cite) => (c.v != null ? `V${c.v}《${byNo.get(c.v)?.title || "?"}》` : undefined),
    href: (c: Cite) => (c.v != null ? citeHref(tid, byNo.get(c.v), c) : undefined),
  }), [tid, byNo]);
  const html = useMemo(() => ({
    answer: renderCited(s.answer, ways),
    sections: s.sections.map((x) => renderCited(x.body, ways)),
  }), [s, ways]);
  const open = v.counts.running + v.counts.queued + v.counts.waiting;
  const go = (e: React.MouseEvent) => {  // a chip is a link: a plain click stays in the page
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    const el = (e.target as Element).closest<HTMLAnchorElement>("a.tp-c");
    if (!el || !citeOf(el)) return;
    e.preventDefault();
    navigate(el.getAttribute("href")!);
  };
  return (
    <article className="tp-answer" onClick={go}>
      <div className="prose lede tp-lede" dangerouslySetInnerHTML={{ __html: html.answer }} />
      {s.first.length > 0 && (
        <section className="tp-first" aria-labelledby="tp-first">
          <h2 className="tp-h" id="tp-first">先看</h2>
          <ol>
            {s.first.map((f) => {
              const it = byNo.get(f.v);
              return it && (
                <li key={f.v}>
                  <a className="tp-f" {...linkProps(`/topics/${tid}/${it.id}`)}>
                    <Thumb src={thumbOf(it.id, "hq")} />
                    <span className="tp-f-t">
                      <b><Words>{it.title || it.id}</Words></b>
                      <span className="tp-f-why">{uncited(f.why)}</span>
                      <small>V{it.no}{it.channel ? ` · ${it.channel}` : ""}{it.duration ? ` · ${len(it.duration)}` : ""}</small>
                    </span>
                  </a>
                </li>
              );
            })}
          </ol>
        </section>
      )}
      {s.sections.map((x, i) => (
        <section key={i} className="tp-sec">
          <h2><Words>{x.title}</Words></h2>
          <div className="prose" dangerouslySetInnerHTML={{ __html: html.sections[i] }} />
        </section>
      ))}
      {s.videos.length > 0 && (
        <section className="tp-adds" aria-labelledby="tp-adds">
          <h2 className="tp-h" id="tp-adds">每期</h2>
          <ol>
            {s.videos.map((x) => {
              const it = byNo.get(x.v);
              return it && (
                <li key={x.v}>
                  <a {...linkProps(`/topics/${tid}/${it.id}`)}>
                    <span className="no num">V{x.v}</span>
                    <span className="t"><span>{uncited(x.adds)}</span><small>{it.title || it.id}</small></span>
                  </a>
                </li>
              );
            })}
          </ol>
        </section>
      )}
      <footer className="tp-sfoot">
        <span>{when(v.synth_at)} 综合 · 依据 {v.synth_n} 期</span>
        {synthing ? <span className="tp-busy">正在重新综合</span> : v.synth_stale && (
          <>
            <span>{open ? `还有 ${open} 期在读，读完会再综合` : "之后又有读完的"}</span>
            <button className="btn sm" onClick={onSynth}>{open ? "现在就综合" : "重新综合"}</button>
          </>
        )}
      </footer>
    </article>
  );
}

/** No answer yet: why, and the way to one */
function NoAnswer({ v, onSynth, synthing }: { v: View; onSynth: () => void; synthing: boolean }) {
  const c = v.counts;
  const open = c.running + c.queued + c.waiting;
  if (synthing) {
    return <div className="tp-none" role="status"><i className="tp-rule" aria-hidden /><h3>正在综合</h3><p>把读完的 {c.done} 期合成一个回答</p></div>;
  }
  if (v.synth_state === "FAILED" || v.synth_state === "WAITING") {
    const [kind, ...rest] = (v.synth_error || "").split(": ");
    const why = v.synth_state === "WAITING" ? `在等模型：${errorLabel(kind)}` : rest.length ? errorLabel(kind) : (v.synth_error || "").slice(0, 120);
    return (
      <div className={`tp-none${v.synth_state === "FAILED" ? " bad" : ""}`} role="status">
        <h3>{v.synth_state === "FAILED" ? "综合没成" : "综合在等"}</h3><p>{why}</p>
        {v.synth_state === "FAILED" && <div className="state-acts"><button className="btn" onClick={onSynth}>再试</button></div>}
      </div>
    );
  }
  if (!c.done) {
    return (
      <div className="tp-none">
        <h3>{open ? "读完的会合在这里" : "没有读完的视频"}</h3>
        <p>{open ? `${c.videos} 期读完后，按子问题合成一个回答，每句标出处` : c.failed ? `${c.failed} 期没读成，在右边重试` : ""}</p>
      </div>
    );
  }
  return (
    <div className="tp-none">
      <h3>读完 {c.done}/{c.videos} 期</h3>
      <p>{open ? "全部读完后自动综合" : "可以综合了"}</p>
      <div className="state-acts"><button className={`btn${open ? "" : " solid"}`} onClick={onSynth}>{open ? `先综合这 ${c.done} 期` : "综合"}</button></div>
    </div>
  );
}

function VideoRow({ it, tid, stop, onRetry }: { it: Item; tid: number; stop?: "paused" | "held"; onRetry: () => void }) {
  const s = itemState(it, stop);
  const done = it.state === "DONE" && !it.excluded;
  const retry = !it.excluded && (it.state === "FAILED" || (it.state === "WAITING" && it.wait !== "TOPIC_QUOTA"));
  return (
    <li className={`tp-v${it.excluded ? " out" : ""}${s.tone === "bad" ? " bad" : ""}`}>
      <a className="tp-v-a" {...linkProps(`/topics/${tid}/${it.id}`)}>
        <span className="tp-v-no num">V{it.no}</span>
        <Thumb src={it.thumb} />
        <span className="tp-v-t">
          <b><Words>{it.title || it.id}</Words></b>
          <small>{[it.channel, len(it.duration)].filter(Boolean).join(" · ")}</small>
          <span className="tp-v-s">
            {!done && !it.excluded && it.state !== "FAILED" && <Phases it={it} />}
            <span className={`tp-fit${done ? ` ${it.fit}` : ""}${s.tone ? ` ${s.tone}` : ""}`}>{s.text}</span>
            {done && it.gist && <span className="tp-gist"> · {uncited(it.gist)}</span>}
          </span>
        </span>
      </a>
      {retry && <button className="btn sm tp-v-btn" onClick={onRetry}>重试</button>}
    </li>
  );
}

function EditSheet({ v, onClose, onSaved }: { v: View; onClose: () => void; onSaved: (view: View) => void }) {
  const t = v.topic;
  const [q, setQ] = useState(t.question);
  const [notes, setNotes] = useState(t.notes || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const sheet = useRef<SheetHandle>(null);
  const changed = q.trim() !== t.question || (notes.trim() || null) !== (t.notes || null);
  const n = v.counts.videos;
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!changed || q.trim().length < 2 || busy) return;
    setBusy(true);
    try { onSaved(await post<View>(`/api/topics/${t.id}`, { question: q.trim(), notes: notes.trim() })); sheet.current?.close(); }
    catch (x) { setErr((x as Error).message); setBusy(false); }
  };
  return (
    <Sheet ref={sheet} title="改问题" onClose={onClose} className="tp-sheet">
      <form className="tp-pick tp-edit" onSubmit={save} onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) save(e); }}>
        <textarea className="tp-q" value={q} rows={2} maxLength={500} onChange={(e) => setQ(e.target.value)} aria-label="问题" data-autofocus />
        <textarea className="tp-notes" value={notes} rows={2} maxLength={4000} onChange={(e) => setNotes(e.target.value)}
          placeholder="要什么、不要什么（可不填）" aria-label="补充" />
        <footer className="tp-pick-foot">
          <p className="tp-sum">{err ? <span className="zhu">{err}</span> : changed ? `按新问题重读 ${n} 期，底稿不重做${v.budget.gemini !== false ? ` · 约用 Gemini 周额度 ${share(n * v.budget.per_video)}` : ""}` : ""}</p>
          <button type="submit" className="btn solid" disabled={!changed || q.trim().length < 2 || busy}>重读</button>
        </footer>
      </form>
    </Sheet>
  );
}

export default function TopicPage({ id }: { id: number }) {
  const [fast, setFast] = useState(true);
  const { data, error, loadedAt, reload, setData } = usePoll<View>(`/api/topics/${id}`, fast ? 4000 : 20000);
  const narrow = useMedia(NARROW);
  const [pane, setPane] = useState<"answer" | "videos" | null>(null);
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const { say, toast } = useToast();
  useRestoreScroll(!!data);
  const c = data?.counts;
  const synthing = data?.synth_state === "QUEUED" || data?.synth_state === "RUNNING";
  useEffect(() => { if (c) setFast(!!(c.running || c.queued || synthing)); }, [c, synthing]);
  useEffect(() => { if (data) document.title = `${data.topic.question} · 专题`; }, [data?.topic.question]); // eslint-disable-line react-hooks/exhaustive-deps
  const byNo = useMemo(() => new Map((data?.items || []).map((it) => [it.no, it])), [data?.items]);
  const have = useMemo(() => new Set((data?.items || []).map((it) => it.id)), [data?.items]);

  if (!data) {
    return <main className="page tp-topic">{error ? (/没有这个专题/.test(error.message) ? <Gone /> : <Fault error={error} retry={reload} />) : <Unroll />}</main>;
  }
  const v = data, t = v.topic, cs = v.counts;
  const open = cs.running + cs.queued + cs.waiting;
  const act = async (path: string, body: unknown = {}, ok?: string) => {
    try { setData(await post<View>(`/api/topics/${id}${path}`, body)); if (ok) say({ text: ok }); }
    catch (e) { say({ text: (e as Error).message, bad: true }); }
  };
  const remove = async () => {
    try { await post(`/api/topics/${id}/delete`); navigate("/topics", { replace: true }); }
    catch (e) { say({ text: (e as Error).message, bad: true }); }
  };
  const held = heldText(v.held);
  const behind = cs.queued + cs.waiting;  // not started, or stopped to wait: a quota down to what topics leave says why
  const status = t.paused ? `已暂停 · 读完 ${cs.done}/${cs.videos}`
    : open ? [cs.running ? `在读 ${cs.running} 期` : "", behind && held ? held : cs.running ? "" : cs.waiting ? `${cs.waiting} 期在等` : "排队",
      `读完 ${cs.done}/${cs.videos}`].filter(Boolean).join(" · ")
    : synthing ? "正在综合" : `读完 ${cs.done} 期`;
  const lamp = cs.running && !t.paused ? "ok" : "idle";
  const shown = pane || (v.synth ? "answer" : "videos");
  const answer = v.synth
    ? <Answer v={v} byNo={byNo} synthing={synthing} onSynth={() => act("/synth", {}, "开始综合")} />
    : <NoAnswer v={v} synthing={synthing} onSynth={() => act("/synth", {}, "开始综合")} />;
  const list = (
    <aside className="tp-side" aria-label="视频">
      <div className="tp-side-h">
        <h2>视频</h2>
        <span className="tp-side-n">{cs.videos} 期{cs.hours ? ` · ${hours(cs.hours)}` : ""}</span>
        <button className="btn ghost sm" onClick={() => setAdding(true)}>加视频</button>
      </div>
      <ol className="tp-vs">
        {v.items.map((it) => <VideoRow key={it.id} it={it} tid={id} stop={t.paused ? "paused" : v.held ? "held" : undefined} onRetry={() => act(`/videos/${it.id}`, { action: "retry" })} />)}
      </ol>
    </aside>
  );
  return (
    <main className="page tp-topic">
      <Stale error={error} loadedAt={loadedAt} />
      <header className="tp-head">
        <a className="crumb" {...linkProps("/topics")}><span aria-hidden>←</span>专题</a>
        <h1><Words>{t.question}</Words></h1>
        {t.notes && <p className="tp-notes-line">{t.notes}</p>}
        <div className="tp-head-b">
          <p className="ph-status">
            <i className={`lamp ${lamp}`} aria-hidden />
            <span>{status}{cs.failed > 0 && <> · <span className="tp-bad">{cs.failed} 期没读成</span></>}</span>
          </p>
          <div className="tp-acts">
            <button className="btn ghost sm" onClick={() => setEditing(true)}>改问题</button>
            {(open > 0 || t.paused) && <button className="btn ghost sm" onClick={() => act("/pause", { paused: !t.paused })}>{t.paused ? "继续读" : "暂停"}</button>}
            <Armed label="删除" sure="确认删除" onGo={remove} />
          </div>
        </div>
      </header>
      {narrow && (
        <div className="seg tp-seg" role="tablist" aria-label="看什么">
          <button role="tab" aria-selected={shown === "answer"} className={shown === "answer" ? "on" : ""} onClick={() => setPane("answer")}>回答</button>
          <button role="tab" aria-selected={shown === "videos"} className={shown === "videos" ? "on" : ""} onClick={() => setPane("videos")}>视频 {cs.videos}</button>
        </div>
      )}
      <div className="tp-body">
        {(!narrow || shown === "answer") && answer}
        {(!narrow || shown === "videos") && list}
      </div>
      {editing && <EditSheet v={v} onClose={() => setEditing(false)} onSaved={(x) => { setData(x); say({ text: "按新问题重读" }); }} />}
      {adding && <Picker budget={v.budget} tid={id} have={have} onClose={() => setAdding(false)} onAdded={(x) => { setData(x); say({ text: "已加进来" }); }} />}
      {toast}
    </main>
  );
}

function Gone() {
  return (
    <div className="state-block">
      <h3>没有这个专题</h3>
      <p>可能已经删除了。</p>
      <div className="state-acts"><a className="btn" {...linkProps("/topics")}>回到专题</a></div>
    </div>
  );
}

