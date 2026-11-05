import { useEffect, useRef, useState } from "react";
import { get } from "../../api";
import { smooth } from "../../util";
import { Adopt, est, fmt, Icon, Track, useNarrow } from "./bits";
import type { Channel, Run, Sample, Workbench } from "./types";

const WAIT: Record<string, string> = {
  NETWORK: "等网络", YOUTUBE_AUTH: "等 YouTube 登录", AGY_AUTH_REQUIRED: "等模型登录", DISK_LOW: "等磁盘空间",
  LIVE_NOT_OVER: "等直播结束", PLAYBOOK_CONFIRMATION: "等定稿", YOUTUBE_REFUSED: "等 YouTube 放行",
};
const stopped = (r: Run) => r.production === "CANCELLED" && r.error_class === "STOPPED";  // 清空 stopped it (a10)
export const done = (r?: Run | null): r is Run & { production: `COMPLETE${string}` } => !!r && r.production.startsWith("COMPLETE");
const live = (r: Run) => ["QUEUED", "RUNNING", "WAITING", "INTERRUPTED"].includes(r.production);

/** The run a cell stands for: the adopted one if it belongs to this version, else one in progress, else the newest. */
export function runFor(s: Sample, pbId: number): Run | null {
  const rs = s.runs.filter((r) => r.playbook_id === pbId);
  return rs.find((r) => r.id === s.adopt.run_id) || rs.find(live) || rs[0] || null;
}

export type MatrixActions = {
  trial: (playbookIds: number[], sourceIds?: string[]) => Promise<void>;
  add: (url: string) => Promise<void>;
  remove: (sourceId: string) => Promise<void>;
  clear: () => Promise<void>;
  retry: (runId: string) => Promise<void>;
};

function AddSample({ cid, onAdd, close }: { cid: string; onAdd: (url: string) => Promise<void>; close: () => void }) {
  const [url, setUrl] = useState("");
  const [cands, setCands] = useState<any[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const out = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) close(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    addEventListener("mousedown", out);
    addEventListener("keydown", esc);
    return () => { removeEventListener("mousedown", out); removeEventListener("keydown", esc); };
  }, [close]);
  const add = async (u: string) => {
    if (!u.trim()) return;
    setBusy(true); setErr(null);
    try { await onAdd(u.trim()); close(); } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  const recent = async () => {
    try { setErr(null); setCands((await get<any>(`/api/channels/${cid}/candidates`)).candidates); } catch (e) { setErr((e as Error).message); }
  };
  return (
    <div className="ch-addpop" ref={ref} role="dialog" aria-label="加样片">
      <input autoFocus value={url} onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add(url)}
        placeholder="粘贴这个频道的视频链接" aria-label="视频链接" />
      <div className="row">
        <button className="ch-btn sm" onClick={recent}>频道最近的视频</button>
        <button className="ch-btn sm solid" disabled={busy || !url.trim()} onClick={() => add(url)}>加为样片</button>
      </div>
      {err && <p className="ch-err">{err}</p>}
      {cands && (
        <div className="ch-cands">
          {cands.length === 0 && <span className="q">读不到最近的视频</span>}
          {cands.map((c) => (
            <button key={c.id} disabled={c.sample || busy} onClick={() => add(`https://www.youtube.com/watch?v=${c.id}`)}>
              <span className="t">{c.title}</span>
              <span className="q">{c.sample ? "已是样片" : c.library ? "库里已有" : c.produced ? "已生产" : ""}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const percent = ([a, b]: [number, number]) => `${b ? Math.round((a / b) * 100) : 4}%`;
const doing = (r: Run) => (r.production === "WAITING" ? (WAIT[r.wait_reason || ""] || "等待") : r.production === "QUEUED" ? "排队中" : "进行中");
/** The version a trial uses by default, as the server has it (tuning.draft_playbook): the column and chip that lead. */
const leadOf = (wb: Workbench) => wb.draft_id ?? wb.playbooks[0]?.id ?? null;
/** What a run came to: its knowledge units, and its 校注 after a red tick; the words are in the cell's tooltip. */
function Tally({ run }: { run: Run }) {
  return <><b>{run.units ?? run.track.length}</b>{run.notes > 0 && <span className="ch-fl">{run.notes}</span>}</>;
}

function Cell({ s, pb, run, queued, sel, cmp, lead, onPick, act }: {
  s: Sample; pb: Workbench["playbooks"][number]; run: Run | null; queued: number; sel: boolean; cmp: boolean; lead: boolean;
  onPick: () => void; act: MatrixActions;
}) {
  const label = `${s.title} · v${pb.version}`;
  if (done(run)) {
    const adopted = s.adopt.run_id === run.id;
    const tip = `${label} · ${run.units ?? run.track.length} 单元 · 校注 ${run.notes}${adopted ? " · 采用" : ""}`;
    return (
      <button className={`ch-cell${sel ? " sel" : cmp ? " cmp" : ""}`} onClick={onPick} aria-pressed={sel} aria-label={tip} title={tip}>
        <Track units={run.track} flags={run.flag_at} dur={s.duration || 0} />
        <span className="cap"><Tally run={run} />{adopted && <Adopt pinned={s.adopt.pinned} small />}</span>
      </button>
    );
  }
  if (run && live(run)) {
    const [a, b] = run.progress;
    const what = `${doing(run)} ${a}/${b}`, eta = run.eta_seconds ? est(run.eta_seconds / 60) : null;
    return (
      <div className="ch-cell dis" aria-label={label} title={eta ? `${what} · ${eta}` : what}>
        <span className="prog"><b style={{ width: percent(run.progress) }} /></span>
        <span className="cap"><b>{what}</b>{eta && <span>{eta}</span>}</span>
      </div>
    );
  }
  if (queued >= 0) {
    return <div className="ch-cell dis" aria-label={label}><span className="qd" /><span className="cap"><span>排队第 {queued + 1}</span></span></div>;
  }
  if (run && stopped(run)) {
    return (
      <div className="ch-cell dis" aria-label={label}>
        <span className="qd" />
        <span className="cap"><span>已停下</span><button className="lnk" onClick={() => act.retry(run.id)}>重试</button></span>
      </div>
    );
  }
  if (run) {  // FAILED / CANCELLED
    return (
      <div className="ch-cell dis" aria-label={label}>
        <span className="qd bad" />
        <span className="cap"><span className="z" title={run.error_line || undefined}>失败{run.error_class ? ` · ${run.error_class.toLowerCase().includes("timeout") ? "超时" : "出错"}` : ""}</span>
          <button className="lnk" onClick={() => act.retry(run.id)}>重试</button></span>
      </div>
    );
  }
  return (
    <div className="ch-cell dis" aria-label={label}>
      <button className={`ch-cplus${lead ? " lead" : ""}`} aria-label={`试跑：${label}`} onClick={() => act.trial([pb.id], [s.id])}><Icon name="play" size={9} fill />试跑</button>
    </div>
  );
}

export type Want = { sample: string; pb: number };
type MatrixProps = {
  wb: Workbench; ch: Channel; sel: { sample: string | null; run: string | null }; cmp: string | null;
  onPick: (sampleId: string, runId: string) => void; act: MatrixActions;
  want?: Want | null; onWant?: (w: Want) => void;
};

/** 样片 × 版本: one cell per sample per playbook version; a cell's track is the run's knowledge units on the video. */
export function Matrix(p: MatrixProps) {
  return useNarrow() ? <Strip {...p} /> : <Grid {...p} />;
}

/** Phones: the samples as a row of cards for one version at a time, the versions as pills underneath.
 *  A card or pill with no finished run asks for it (want) instead of falling back to another version. */
function Strip({ wb, ch, sel, onPick, act, want, onWant }: MatrixProps) {
  const pbs = wb.playbooks;
  const selRun = wb.samples.flatMap((s) => s.runs).find((r) => r.id === sel.run);
  const [adding, setAdding] = useState(false);
  const pb = pbs.find((p) => p.id === (want?.pb ?? selRun?.playbook_id)) || pbs[0] || null;
  const sample = wb.samples.find((s) => s.id === (want?.sample ?? sel.sample)) || null;
  const pickVersion = (id: number) => {
    if (!sample) return;
    const r = runFor(sample, id);
    if (done(r)) onPick(sample.id, r.id); else onWant?.({ sample: sample.id, pb: id });
  };
  return (
    <section className="ch-strip" aria-label="样片与版本">
      <div className="ch-sts">
        {wb.samples.map((s) => {
          const run = pb ? runFor(s, pb.id) : null;
          const queued = pb ? wb.queue.findIndex((q) => q.source_id === s.id && q.playbook_id === pb.id) : -1;
          const head = <b className="tt">{s.title}</b>;
          if (done(run)) {
            const on = !want && run.id === sel.run;
            return (
              <button key={s.id} className={`ch-stc${on ? " on" : ""}`} aria-pressed={on} onClick={() => onPick(s.id, run.id)}>
                {head}<Track units={run.track} flags={run.flag_at} dur={s.duration || 0} />
                <span className="mm"><span>{fmt(s.duration)}</span><span>{run.units ?? run.track.length} 单元</span>{s.adopt.run_id === run.id && <Adopt pinned={s.adopt.pinned} small />}</span>
              </button>
            );
          }
          const [a, b] = run && live(run) ? run.progress : [0, 0];
          const on = !!want && want.sample === s.id;
          return (
            <button key={s.id} className={`ch-stc dis${on ? " on" : ""}`} aria-pressed={on} onClick={() => pb && onWant?.({ sample: s.id, pb: pb.id })}>
              {head}
              {run && live(run) ? <span className="prog"><b style={{ width: `${b ? Math.round((a / b) * 100) : 4}%` }} /></span> : <span className={`qd${run && queued < 0 ? " bad" : ""}`} />}
              <span className="mm"><span>{fmt(s.duration)}</span>
                {run && live(run) ? <span>{run.production === "QUEUED" ? "排队中" : "进行中"} {a}/{b}</span>
                  : queued >= 0 ? <span>排队第 {queued + 1}</span> : run ? <span className="z">失败</span> : <span>没跑过</span>}</span>
            </button>
          );
        })}
        <button className="ch-stc add" aria-expanded={adding} onClick={() => setAdding(!adding)}><Icon name="plus" size={14} /><span>加样片</span></button>
      </div>
      {adding && <AddSample cid={ch.id} onAdd={act.add} close={() => setAdding(false)} />}
      <div className="ch-stv" role="radiogroup" aria-label="版本">
        {pbs.map((p) => {
          const adopted = !!sample && runFor(sample, p.id)?.id === sample.adopt.run_id;
          return (
            <button key={p.id} role="radio" aria-checked={p.id === pb?.id} className={`ch-pill${p.id === pb?.id ? " on" : ""}`} onClick={() => pickVersion(p.id)}>
              v{p.version}{p.id === wb.draft_id && <small>草稿</small>}{p.status === "ACTIVE" && <small>生效中</small>}
              {adopted && <Adopt pinned={!!sample?.adopt.pinned} small />}
            </button>
          );
        })}
        {pb && pb.missing > 0 && <button className="ch-vrun" onClick={() => act.trial([pb.id])}><Icon name="play" size={9} fill />试跑 {pb.missing} 期</button>}
      </div>
    </section>
  );
}

/** Phones: what the preview shows for a sample and version that has no finished run — its state and the way forward. */
export function RunPending({ wb, want, act, switcher }: { wb: Workbench; want: Want; act: MatrixActions; switcher: React.ReactNode }) {
  const s = wb.samples.find((x) => x.id === want.sample);
  const pb = wb.playbooks.find((p) => p.id === want.pb);
  if (!s || !pb) return null;
  const run = runFor(s, pb.id);
  const queued = wb.queue.findIndex((q) => q.source_id === s.id && q.playbook_id === pb.id);
  const [a, b] = run && live(run) ? run.progress : [0, 0];
  return (
    <div className="ch-pane ch-nt">
      <div className="ch-ph">{switcher}<span className="ch-chip sm">v{pb.version}</span></div>
      <div className="ch-pw ch-pend" role="status">
        <b>{s.title}</b>
        {run && live(run) ? (
          <><span className="ch-prog" style={{ width: 160 }}><b style={{ width: percent(run.progress) }} /></span>
            <span>{doing(run)} {a}/{b}{run.eta_seconds ? ` · ${est(run.eta_seconds / 60)}` : ""}</span></>
        ) : queued >= 0 ? <span>v{pb.version} 排队第 {queued + 1}</span>
          : run && stopped(run) ? (<><span>v{pb.version} 已停下</span><button className="ch-btn sm" onClick={() => act.retry(run.id)}>重试</button></>)
          : run ? (<><span className="z" title={run.error_line || undefined}>v{pb.version} 没有跑完{run.error_line ? `：${run.error_line}` : ""}</span>
            <button className="ch-btn sm" onClick={() => act.retry(run.id)}>重试</button></>)
          : (<><span>v{pb.version} 还没在这期上跑过</span>
            <button className="ch-btn sm solid" onClick={() => act.trial([pb.id], [s.id])}><Icon name="play" size={9} fill />试跑</button></>)}
      </div>
    </div>
  );
}

/** What a version's note says beyond the note of the version before: a line of revisions shares its start ("原版修订：…·资料收紧"
 *  then "原版修订：…·资料收紧·全流程完整"), and only the end tells columns apart. The whole note stays in the tooltip. */
function noteDelta(note: string, prev?: string | null) {
  let i = 0;
  while (prev && i < note.length && i < prev.length && note[i] === prev[i]) i++;
  if (i === note.length) i = 0;  // nothing beyond it: the note as it is
  // only whole clauses are shared: 原话模式 and 原版修订 have a character in common, not a start
  const sep = /[\s·・:：,，、;；/|-]/;
  while (i > 0 && !sep.test(note[i]) && !sep.test(note[i - 1])) i--;
  return note.slice(i).replace(/^[\s·・:：,，、;；/|-]+/, "").replace(/^v\d+\s+/i, "") || note;
}
const LEAD_W = 230, COL_W = 118, NEW_W = 132;  // no column below what its own cell and heading need; past that the table scrolls

/** The overview: which sample has been run on which version, and which version each one will deliver. The version being
 *  tuned leads. Older versions come in with 全部 N 版 unless something rests on them: an issue adopts one, or it runs. */
function Grid({ wb, ch, sel, cmp, onPick, act }: MatrixProps) {
  const [all, setAll] = useState(false);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [clearing, setClearing] = useState<number | null>(null);
  const [asking, setAsking] = useState<number | null>(null);  // an older version's 试跑 takes a second click
  const pbs = wb.playbooks;
  const lead = leadOf(wb);
  const before = pbs.find((p) => p.id !== lead && wb.samples.some((s) => s.runs.some((r) => r.playbook_id === p.id)));
  const keep = new Set<number | null | undefined>([lead, before?.id, wb.active_id, ...wb.queue.map((q) => q.playbook_id)]);
  for (const s of wb.samples) for (const r of s.runs) if (r.id === s.adopt.run_id || r.id === sel.run || live(r)) keep.add(r.playbook_id);
  const cols = all ? pbs : pbs.filter((p) => keep.has(p.id));
  const foldable = pbs.some((p) => !keep.has(p.id));
  const compiling = ch.compile?.state === "running";
  const widths = [...(compiling ? [NEW_W] : []), ...cols.map((p) => (p.id === lead ? LEAD_W : COL_W))];
  // every row is as wide as the sum, or the table if that is wider: the columns line up whatever a row holds
  const grid = {
    gridTemplateColumns: `minmax(250px, 1.1fr) ${widths.map((w) => `minmax(${w}px, ${w === LEAD_W ? 1.25 : 1}fr)`).join(" ") || `minmax(${COL_W}px, 1fr)`}`,
    minWidth: 250 + (widths.reduce((a, b) => a + b, 0) || COL_W),
  };
  return (
    <section className={`ch-tb${all ? " all" : ""}`} aria-label="样片与版本">
      <div className="ch-tbx">
        <div className="ch-tbr ch-tbh" style={grid}>
          <div className="c0">
            <span>样片</span>
            <button className="ch-addbtn" aria-expanded={adding} onClick={() => setAdding(!adding)}><Icon name="plus" size={12} /><span>加样片</span></button>
            {foldable && <button className="ch-lk grow" aria-expanded={all} onClick={() => setAll(!all)}>{all ? "收起" : `全部 ${pbs.length} 版`}</button>}
          </div>
          {compiling && <div className="vh"><span className="vh1"><span className="vl">新版本</span><span className="ch-chip sm">编译中</span></span><span className="vm">约 3 分钟</span></div>}
          {cols.map((p) => {
            const queued = wb.queue.filter((q) => q.playbook_id === p.id).length;
            const running = wb.samples.reduce((n, s) => n + s.runs.filter((r) => r.playbook_id === p.id && live(r)).length, 0);
            const busy = queued + running > 0, plan = `试跑 ${p.missing} 期 · ${est(p.est_minutes)}`, confirming = busy && clearing === p.id;
            const load = [running && `在跑 ${running}`, queued && `排队 ${queued}`].filter(Boolean).join(" · ");
            if (asking === p.id && p.missing > 0 && !busy) {
              return (
                <div key={p.id} className="vh">
                  <span className="vh1"><span className="vl">v{p.version}</span><span className="vn">{est(p.est_minutes)}</span></span>
                  <span className="vm">
                    <button className="ch-vrun" onClick={() => { setAsking(null); act.trial([p.id]); }}>确认 {p.missing} 期</button>
                    <button className="lnk" onClick={() => setAsking(null)}>取消</button>
                  </span>
                </div>
              );
            }
            return (
              <div key={p.id} className="vh">
                <span className="vh1">
                  <span className="vl">v{p.version}</span>
                  {p.id === wb.draft_id && <span className="ch-chip sm">草稿</span>}
                  {p.status === "ACTIVE" && <span className="ch-chip sm ink">生效中</span>}
                  {p.note && <span className="vn" title={p.note}>{noteDelta(p.note, pbs[pbs.indexOf(p) + 1]?.note)}</span>}
                </span>
                <span className="vm">
                  {p.adopt_count > 0 && !confirming && <span className="ad-count" title={`${p.adopt_count} 期采用这一版`}><span className="ch-ad sm"><Icon name="check" size={8} /></span>{p.adopt_count}</span>}
                  {busy && (
                    // 清空 takes the place of 试跑 N 期 the moment it is clicked: a second click must not drop the whole queue;
                    // it stops what runs as well (a10)
                    <span className="ch-vq">{confirming
                      ? <><button onClick={() => { setClearing(null); act.clear(); }}>确认清空</button><button onClick={() => setClearing(null)}>取消</button></>
                      : <><span className="n" title={load}>{load}</span><button onClick={() => setClearing(p.id)}>清空</button></>}</span>
                  )}
                  {p.missing > 0 && (p.id === lead
                    ? <button className="ch-vrun" title={plan} onClick={() => act.trial([p.id])}><Icon name="play" size={9} fill />{busy ? `${p.missing} 期` : plan}</button>
                    : !busy && <button className="ch-vrun ol" title={plan} onClick={() => setAsking(p.id)}><Icon name="play" size={9} fill />{p.missing} 期</button>)}
                </span>
              </div>
            );
          })}
          {!cols.length && <div className="vh q">还没有规程版本</div>}
        </div>
        {wb.samples.map((s) => (
          <div key={s.id} className="ch-tbr" style={grid}>
            <div className="ch-tbs">
              <span className="stt" title={s.title}>{s.title}</span>
              {s.notion_url && <span className="sti" role="img" aria-label="库里已有" title="库里已有"><Icon name="db" size={11} /></span>}
              {s.busy && <span className="stb" role="img" aria-label="试跑中" title="试跑中" />}
              <em className="std">{fmt(s.duration)}</em>
              {removing === s.id ? (
                <span className="ch-rmc">
                  <button className="ch-btn xs" onClick={() => setRemoving(null)}>取消</button>
                  <button className="ch-btn xs solid" onClick={async () => { setRemoving(null); await act.remove(s.id); }}>移出</button>
                </span>
              ) : (
                <button className="rm" aria-label={`移出样片：${s.title}`} onClick={() => setRemoving(s.id)}><Icon name="x" size={13} /></button>
              )}
            </div>
            {compiling && <div className="ch-cell dis"><span className="qd shim" /><span className="cap"><span>等编译</span></span></div>}
            {cols.map((p) => {
              const run = runFor(s, p.id);
              const queued = wb.queue.findIndex((q) => q.source_id === s.id && q.playbook_id === p.id);
              return <Cell key={p.id} s={s} pb={p} run={run} queued={queued} sel={!!run && sel.run === run.id} cmp={!!run && cmp === run.id}
                lead={p.id === lead} onPick={() => run && onPick(s.id, run.id)} act={act} />;
            })}
            {!cols.length && <div className="ch-cell dis" />}
          </div>
        ))}
        {!wb.samples.length && <div className="ch-tbempty">先加两三期有代表性的视频做样片，再用规程试跑。</div>}
      </div>
      {adding && <AddSample cid={ch.id} onAdd={act.add} close={() => setAdding(false)} />}
    </section>
  );
}

/** The cell being looked at, over the three panes: the table is for taking stock, this is for going through it. It stays
 *  under the masthead as the page scrolls. With the focus on it, ←→ change the version and ↑↓ the sample. */
export function Current({ wb, s, run, cmp, onPick, act, barRef }: {
  wb: Workbench; s: Sample; run: Run; cmp: string | null; onPick: (sampleId: string, runId: string) => void; act: MatrixActions;
  barRef: React.RefObject<HTMLElement | null>;
}) {
  const chips = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(false);
  useEffect(() => {  // 全部样片 is for when the table has scrolled away
    let raf = 0;
    const check = () => {
      raf = 0;
      const el = barRef.current;
      if (el) setStuck(el.getBoundingClientRect().top <= parseFloat(getComputedStyle(el).top) + 1);
    };
    const on = () => { if (!raf) raf = requestAnimationFrame(check); };
    check();
    addEventListener("scroll", on, { passive: true });
    addEventListener("resize", on);
    return () => { cancelAnimationFrame(raf); removeEventListener("scroll", on); removeEventListener("resize", on); };
  }, [barRef]);
  useEffect(() => {  // with more versions than fit, the chosen one is on screen; scrollIntoView could move the page as well
    const box = chips.current, el = box?.querySelector<HTMLElement>(".on");
    if (!box || !el) return;
    const left = el.offsetLeft, right = left + el.offsetWidth;
    if (left < box.scrollLeft) box.scrollTo({ left, behavior: smooth() });
    else if (right > box.scrollLeft + box.clientWidth) box.scrollTo({ left: right - box.clientWidth, behavior: smooth() });
  }, [run.id]);
  const list = wb.samples.filter((x) => x.runs.some(done));
  const i = list.findIndex((x) => x.id === s.id);
  const lead = leadOf(wb);
  // a version's finished run on this sample: the one on screen if it is this version's, then the adopted one, then the newest
  const finished = (pid: number) => {
    const rs = s.runs.filter((r) => r.playbook_id === pid && done(r));
    return rs.find((r) => r.id === run.id) || rs.find((r) => r.id === s.adopt.run_id) || rs[0] || null;
  };
  const ready = wb.playbooks.map((p) => finished(p.id)).filter((r) => !!r);
  // the next sample on the same version where it has that one, else what it adopts, else its newest
  const step = (d: number) => {
    const t = list[i + d];
    if (!t) return;
    const rs = t.runs.filter(done), same = rs.filter((r) => r.playbook_id === run.playbook_id), adopted = (r: Run) => r.id === t.adopt.run_id;
    const r = same.find(adopted) || same[0] || rs.find(adopted) || rs[0];
    onPick(t.id, r.id);
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (e.key === "ArrowUp" || e.key === "ArrowDown") step(e.key === "ArrowUp" ? -1 : 1);
    else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      const r = ready[ready.findIndex((x) => x.id === run.id) + (e.key === "ArrowLeft" ? -1 : 1)];
      if (r) onPick(s.id, r.id);
    } else return;
    e.preventDefault();
    barRef.current?.focus({ preventScroll: true });  // the chip that had it may be gone with the sample
  };
  return (
    <section className={`ch-cur${stuck ? " stuck" : ""}`} ref={barRef} tabIndex={-1} aria-label="当前样片" onKeyDown={onKey}>
      <span className="ch-curn">
        <button className="ch-icb prev" aria-label="上一期" aria-disabled={i <= 0} onClick={() => step(-1)}><Icon name="chev" size={14} /></button>
        <span>{i + 1}/{list.length}</span>
        <button className="ch-icb" aria-label="下一期" aria-disabled={i >= list.length - 1} onClick={() => step(1)}><Icon name="chev" size={14} /></button>
      </span>
      <b className="ch-curt" title={s.title}>{s.title}</b>
      <em className="ch-curd">{fmt(s.duration)}</em>
      <div className="ch-curs" ref={chips} role="group" aria-label="版本">
        {wb.playbooks.map((p) => {
          const r = finished(p.id);
          if (r) {
            const on = r.id === run.id, adopted = s.adopt.run_id === r.id;
            const tip = `v${p.version} · ${r.units ?? r.track.length} 单元 · 校注 ${r.notes}${adopted ? " · 采用" : ""}`;
            return (
              <button key={p.id} className={`ch-curc${on ? " on" : cmp === r.id ? " cmp" : ""}`} aria-pressed={on} aria-label={tip} title={tip} onClick={() => onPick(s.id, r.id)}>
                <Track units={r.track} flags={r.flag_at} dur={s.duration || 0} />
                <span className="cap"><b className="v">v{p.version}</b><Tally run={r} />{adopted && <Adopt pinned={s.adopt.pinned} small />}</span>
              </button>
            );
          }
          const rs = s.runs.filter((x) => x.playbook_id === p.id), going = rs.find(live);
          const queued = wb.queue.findIndex((q) => q.source_id === s.id && q.playbook_id === p.id);
          if (going) {
            const [a, b] = going.progress;
            return (
              <div key={p.id} className="ch-curc st" title={`${doing(going)} ${a}/${b}${going.eta_seconds ? ` · ${est(going.eta_seconds / 60)}` : ""}`}>
                <span className="ch-prog"><b style={{ width: percent(going.progress) }} /></span>
                <span className="cap"><b className="v">v{p.version}</b><span>{a}/{b}</span></span>
              </div>
            );
          }
          if (queued >= 0) {
            return <div key={p.id} className="ch-curc st"><span className="qd" /><span className="cap"><b className="v">v{p.version}</b><span>排队第 {queued + 1}</span></span></div>;
          }
          if (p.id !== lead) return null;
          return rs[0]
            ? <button key={p.id} className="ch-curc try" title={rs[0].error_line || undefined} onClick={() => act.retry(rs[0].id)}>重试 v{p.version}</button>
            : <button key={p.id} className="ch-curc try" onClick={() => act.trial([p.id], [s.id])}><Icon name="play" size={9} fill />试跑 v{p.version}</button>;
        })}
      </div>
      <button className="ch-curu" onClick={() => scrollTo({ top: 0, behavior: smooth() })}><Icon name="chevd" size={12} />全部样片</button>
    </section>
  );
}
