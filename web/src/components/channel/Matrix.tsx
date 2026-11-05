import { useEffect, useRef, useState } from "react";
import { get } from "../../api";
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

function Cell({ s, pb, run, queued, sel, cmp, onPick, act }: {
  s: Sample; pb: Workbench["playbooks"][number]; run: Run | null; queued: number; sel: boolean; cmp: boolean;
  onPick: () => void; act: MatrixActions;
}) {
  const label = `${s.title} · v${pb.version}`;
  if (done(run)) {
    const adopted = s.adopt.run_id === run.id;
    return (
      <button className={`ch-cell${sel ? " sel" : cmp ? " cmp" : ""}`} onClick={onPick} aria-pressed={sel} aria-label={label}>
        <Track units={run.track} flags={run.flag_at} dur={s.duration || 0} />
        <span className="cap"><b>{run.units ?? run.track.length} 单元</b><span>校注 {run.notes}</span>{adopted && <Adopt pinned={s.adopt.pinned} />}</span>
      </button>
    );
  }
  if (run && live(run)) {
    const [a, b] = run.progress;
    const what = run.production === "WAITING" ? (WAIT[run.wait_reason || ""] || "等待") : run.production === "QUEUED" ? "排队中" : "进行中";
    return (
      <div className="ch-cell dis" aria-label={label}>
        <span className="prog"><b style={{ width: `${b ? Math.round((a / b) * 100) : 4}%` }} /></span>
        <span className="cap"><b>{what} {a}/{b}</b>{run.eta_seconds ? <span>{est(run.eta_seconds / 60)}</span> : null}</span>
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
      <button className="ch-cplus" onClick={() => act.trial([pb.id], [s.id])}><Icon name="play" size={9} fill />试跑</button>
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
          <><span className="ch-prog" style={{ width: 160 }}><b style={{ width: `${b ? Math.round((a / b) * 100) : 4}%` }} /></span>
            <span>{run.production === "WAITING" ? (WAIT[run.wait_reason || ""] || "等待") : run.production === "QUEUED" ? "排队中" : "进行中"} {a}/{b}{run.eta_seconds ? ` · ${est(run.eta_seconds / 60)}` : ""}</span></>
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

function Grid({ wb, ch, sel, cmp, onPick, act }: MatrixProps) {
  const [all, setAll] = useState(false);
  const [folded, setFolded] = useState(false);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [clearing, setClearing] = useState<number | null>(null);
  const pbs = wb.playbooks;
  const adoptedPb = new Set(wb.samples.map((s) => s.runs.find((r) => r.id === s.adopt.run_id)?.playbook_id));
  const cols = all ? pbs : pbs.filter((p, i) => i < 3 || adoptedPb.has(p.id));
  const hidden = pbs.length - cols.length;
  const compiling = ch.compile?.state === "running";
  const rows = folded ? wb.samples.filter((s) => s.id === sel.sample) : wb.samples;
  const grid = { gridTemplateColumns: `minmax(0, 300px) ${compiling ? "minmax(0, 1fr) " : ""}repeat(${Math.max(1, cols.length)}, minmax(0, 1fr)) 84px` };
  return (
    <section className="ch-tb" aria-label="样片与版本">
      <div className="ch-tbr ch-tbh" style={grid}>
        <div className="c0">
          <span>样片</span>
          <button className="ch-addbtn" aria-expanded={adding} onClick={() => setAdding(!adding)}><Icon name="plus" size={12} /><span>加样片</span></button>
          <button className="ch-icb grow" aria-label={folded ? "展开全部样片" : "只看当前样片"} onClick={() => setFolded(!folded)}>
            <span style={{ display: "inline-flex", transform: folded ? "rotate(180deg)" : undefined }}><Icon name="chevd" size={14} /></span>
          </button>
        </div>
        {compiling && <div className="vh"><span className="vh1"><span className="vl">新版本</span><span className="ch-chip sm">编译中</span></span><span className="vm">约 3 分钟</span></div>}
        {cols.map((p) => {
          const queued = wb.queue.filter((q) => q.playbook_id === p.id).length;
          const running = wb.samples.reduce((n, s) => n + s.runs.filter((r) => r.playbook_id === p.id && live(r)).length, 0);
          return (
            <div key={p.id} className="vh">
              <span className="vh1">
                <span className="vl">v{p.version}</span>
                {p.id === wb.draft_id && <span className="ch-chip sm">草稿</span>}
                {p.status === "ACTIVE" && <span className="ch-chip sm ink">生效中</span>}
                {p.note && <span className="vn" title={p.note}>{p.note}</span>}
              </span>
              <span className="vm">
                <span className="ad-count"><Adopt pinned={false} small />采用 {p.adopt_count}/{wb.samples.length}</span>
                {p.missing > 0 && (
                  <button className="ch-vrun" onClick={() => act.trial([p.id])}><Icon name="play" size={9} fill />试跑 {p.missing} 期 · {est(p.est_minutes)}</button>
                )}
                {queued + running > 0 && (
                  // 清空 takes the place of 试跑 N 期 the moment it is clicked: a second click must not drop the whole queue;
                  // it stops what runs as well (a10)
                  <span className="ch-vq"><span>{[running && `在跑 ${running}`, queued && `排队 ${queued}`].filter(Boolean).join(" · ")}</span>{clearing === p.id
                    ? <><button onClick={() => { setClearing(null); act.clear(); }}>确认清空</button><button onClick={() => setClearing(null)}>取消</button></>
                    : <button onClick={() => setClearing(p.id)}>清空</button>}</span>
                )}
              </span>
            </div>
          );
        })}
        {!cols.length && <div className="vh q">还没有规程版本</div>}
        <button className="older" disabled={!hidden && !all} onClick={() => setAll(!all)}>
          {all ? "收起" : hidden ? <>更早 {hidden}<Icon name="chev" size={12} /></> : ""}
        </button>
      </div>
      {adding && <AddSample cid={ch.id} onAdd={act.add} close={() => setAdding(false)} />}
      {rows.map((s) => (
        <div key={s.id} className="ch-tbr" style={grid}>
          <div className="ch-tbs">
            <span className="ch-thumb"><em>{fmt(s.duration)}</em></span>
            <span className="txt">
              <span className="stt" title={s.title}>{s.title}</span>
              <span className="stm">{s.notion_url && <span>库里已有</span>}{s.busy && <span>试跑中</span>}</span>
            </span>
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
            const queued = wb.queue.filter((q) => q.source_id === s.id && q.playbook_id === p.id).length ? wb.queue.findIndex((q) => q.source_id === s.id && q.playbook_id === p.id) : -1;
            return <Cell key={p.id} s={s} pb={p} run={run} queued={queued} sel={!!run && sel.run === run.id} cmp={!!run && cmp === run.id}
              onPick={() => run && onPick(s.id, run.id)} act={act} />;
          })}
          {!cols.length && <div className="ch-cell dis" />}
          <div className="older" />
        </div>
      ))}
      {!wb.samples.length && <div className="ch-tbempty">先加两三期有代表性的视频做样片，再用规程试跑。</div>}
    </section>
  );
}
