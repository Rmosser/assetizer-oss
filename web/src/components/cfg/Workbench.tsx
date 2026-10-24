import { useEffect, useState } from "react";
import { get, post } from "../../api";
import { usePoll } from "../../hooks";
import { linkProps } from "../../router";
import { PRODUCTION, WAIT, ago } from "../../util";
import { Chip, Empty, Note, PB, day, mins } from "./ui";

type Run = {
  id: string; production: string; stage: string | null; wait_reason: string | null; error_class: string | null;
  created_at: string; finished_at: string | null; playbook_id: number | null; version: number | null; playbook_status: string | null;
  progress: [number, number]; units: number | null; must: number; clips: number; notes: number; chars: number;
  liked: boolean; note: string | null;
};
type Sample = {
  id: string; title: string; url: string; duration: number | null; published_at: string | null; added_at: string;
  current_run_id: string | null; delivery: string; notion_url: string | null; runs: Run[]; stale: boolean; busy: boolean;
};
type WB = {
  channel: { id: string; name: string; stage: string; mode: string; youtube_channel_id: string | null };
  playbooks: { id: number; version: number; status: string; prompt_version: number | null; note: string | null; created_at: string }[];
  draft_id: number | null; active_id: number | null; samples: Sample[];
  queue: { id: number; source_id: string; playbook_id: number; version: number; title: string | null }[];
  summary: { playbook_id: number; version: number; note: string | null; done: number; samples: number; liked: number; notes: number }[];
};

const done = (r: Run) => r.production.startsWith("COMPLETE");
const live = (r: Run) => ["QUEUED", "RUNNING", "WAITING", "INTERRUPTED"].includes(r.production);

function RunState({ r }: { r: Run }) {
  if (r.production === "RUNNING") {
    const [a, b] = r.progress;
    return <span className="wb-prog" title={r.stage || ""}><i style={{ "--w": `${b ? (a / b) * 100 : 0}%` } as React.CSSProperties} /><span>生产中 {a}/{b}</span></span>;
  }
  if (r.production === "WAITING") return <Chip tone="warn" title={r.wait_reason || ""}>等待 · {WAIT[r.wait_reason || ""] || r.wait_reason}</Chip>;
  if (r.production === "FAILED") return <Chip tone="bad" title={r.error_class || ""}>失败</Chip>;
  if (r.production === "COMPLETE_PARTIAL") return <Chip tone="warn">完成 · 有告警</Chip>;
  if (r.production === "COMPLETE") return <Chip tone="ok">完成</Chip>;
  return <Chip>{PRODUCTION[r.production] || r.production}</Chip>;
}

function Compare({ a, b, onClose }: { a: string; b: string; onClose: () => void }) {
  const [c, setC] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [onlyDiff, setOnlyDiff] = useState(false);
  useEffect(() => { setC(null); setErr(null); get(`/api/compare?a=${a}&b=${b}`).then(setC).catch((e) => setErr(e.message)); }, [a, b]);
  const tc = (t: number) => `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(Math.round(t % 60)).padStart(2, "0")}`;
  if (err) return <Note tone="bad">{err}</Note>;
  if (!c) return <Note>正在对齐两个版本的知识单元……</Note>;
  const rows = [["units", "知识单元"], ["must", "必学"], ["clips", "片段"], ["notes", "校注"], ["chars", "字数"]] as const;
  const pairs = onlyDiff ? c.pairs.filter((p: any) => !p.a || !p.b) : c.pairs;
  return (
    <div className="wb-cmp">
      <div className="wb-cmp-h">
        <b>v{c.a.version} → v{c.b.version}</b>
        <span className="muted">两版都有 {c.matched} · 只在 v{c.a.version} {c.pairs.filter((p: any) => !p.b).length} · 只在 v{c.b.version} {c.pairs.filter((p: any) => !p.a).length}</span>
        <button className="btn xs ghost" onClick={onClose}>关闭</button>
      </div>
      <div className="wb-cmp-grid">
        <table className="tbl tight">
          <thead><tr><th>指标</th><th className="r">v{c.a.version}</th><th className="r">v{c.b.version}</th><th className="r">变化</th></tr></thead>
          <tbody>
            {rows.map(([k, l]) => {
              const x = c.a[k] ?? 0, y = c.b[k] ?? 0, d = y - x;
              return <tr key={k}><td>{l}</td><td className="r num">{x.toLocaleString()}</td><td className="r num">{y.toLocaleString()}</td>
                <td className={`r num ${d > 0 ? "up" : d < 0 ? "down" : "muted"}`}>{d > 0 ? `+${d.toLocaleString()}` : d ? d.toLocaleString() : "—"}</td></tr>;
            })}
          </tbody>
        </table>
        <div className="wb-rules">
          <div className="label">规则变化</div>
          {c.rules_added.length + c.rules_removed.length === 0 && <p className="muted">两版规则相同。</p>}
          {c.rules_added.map((r: string) => <p key={`+${r}`} className="add"><b>+</b>{r}</p>)}
          {c.rules_removed.map((r: string) => <p key={`-${r}`} className="del"><b>−</b>{r}</p>)}
        </div>
      </div>
      <div className="wb-cmp-bar">
        <span className="label">知识单元（按时间对齐）</span>
        <label className="chk"><input type="checkbox" checked={onlyDiff} onChange={(e) => setOnlyDiff(e.target.checked)} />只看差异</label>
      </div>
      <div className="tbl-wrap">
        <table className="tbl">
          <thead><tr><th style={{ width: "4.5em" }}>时间</th><th>v{c.a.version}</th><th>v{c.b.version}</th></tr></thead>
          <tbody>
            {pairs.map((p: any, i: number) => (
              <tr key={i}>
                <td className="num muted">{tc((p.a || p.b).start)}</td>
                {[p.a, p.b].map((u: any, j: number) => (
                  <td key={j} className={u ? "" : "gap"}>
                    {u ? <>{u.priority === 3 && <b className="must" title="必学">★</b>}{u.title}{u.notes > 0 && <span className="wb-n">校注 {u.notes}</span>}</> : "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Candidates({ cid, onAdd, busy }: { cid: string; onAdd: (ids: string[]) => void; busy: boolean }) {
  const [list, setList] = useState<any[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [pick, setPick] = useState<Set<string>>(new Set());
  const load = async () => {
    setLoading(true); setErr(null);
    try { setList((await get<any>(`/api/channels/${cid}/candidates`)).candidates); } catch (e) { setErr((e as Error).message); }
    setLoading(false);
  };
  if (!list) {
    return (
      <div className="wb-cand-load">
        <button className="btn sm" disabled={loading} onClick={load}>{loading ? "正在读取频道最近的视频……" : "列出频道最近的视频"}</button>
        {err && <Note tone="bad">{err}</Note>}
      </div>
    );
  }
  const toggle = (id: string) => setPick((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  return (
    <div className="wb-cands">
      <div className="tbl-wrap">
        <table className="tbl">
          <thead><tr><th style={{ width: "2.2em" }} /><th>视频</th><th style={{ width: "7em" }}>发布</th><th style={{ width: "6em" }}>时长</th><th style={{ width: "9em" }}>状态</th></tr></thead>
          <tbody>
            {list.map((x) => (
              <tr key={x.id} className={x.sample ? "dim" : ""} onClick={() => !x.sample && toggle(x.id)}>
                <td><input type="checkbox" aria-label={`选择 ${x.title}`} disabled={x.sample} checked={x.sample || pick.has(x.id)}
                  onChange={() => toggle(x.id)} onClick={(e) => e.stopPropagation()} /></td>
                <td>{x.title}</td>
                <td className="num muted">{(x.published || "").slice(0, 10)}</td>
                <td className="num muted">{x.duration ? mins(x.duration) : "—"}</td>
                <td className="tags">
                  {x.sample && <Chip>已是样片</Chip>}
                  {x.library && <a className="chip ok" href={x.library.url} target="_blank" rel="noreferrer" title={x.library.title} onClick={(e) => e.stopPropagation()}>库里已有 ↗</a>}
                  {x.produced && !x.sample && <Chip>已生产过</Chip>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="wb-cands-bar">
        <button className="btn sm solid" disabled={!pick.size || busy} onClick={() => {
          onAdd([...pick]);
          setList(list.map((x) => (pick.has(x.id) ? { ...x, sample: true } : x)));
          setPick(new Set());
        }}>加入选中的 {pick.size} 期</button>
        <button className="btn sm ghost" onClick={load}>刷新</button>
      </div>
    </div>
  );
}

/** 频道调试台 (design §R2-1): samples → trial with a version → compare → edit the prompt → rerun → 定稿. */
export default function Workbench({ cid, stage, onChanged }: { cid: string; stage: string; onChanged: () => void }) {
  const [noteFor, setNoteFor] = useState<string | null>(null);
  const [noteText, setNoteText] = useState("");
  const [interval, setIntervalMs] = useState(0);
  const { data: wb, error, reload } = usePoll<WB>(`/api/channels/${cid}/workbench`, interval);
  const [pb, setPb] = useState<number | null>(null);
  const [url, setUrl] = useState("");
  const [runNow, setRunNow] = useState(true);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [cmp, setCmp] = useState<{ sid: string; a: string; b: string } | null>(null);
  useEffect(() => { setIntervalMs(wb?.samples.some((s) => s.busy) ? 15000 : 0); }, [wb]);
  useEffect(() => { if (wb && pb == null) setPb(wb.draft_id); }, [wb, pb]);
  useEffect(() => { reload(); }, [stage, reload]);
  if (error && !wb) return <Note tone="bad">{error.message}</Note>;
  if (!wb) return <Note>正在读取调试台……</Note>;
  const tuning = wb.channel.stage === "TUNING";
  const chosen = wb.playbooks.find((p) => p.id === pb);
  const vnote = (pid: number | null) => wb.playbooks.find((p) => p.id === pid)?.note;
  const vote = (r: Run, liked: boolean, note: string | null) => act(() => post(`/api/runs/${r.id}/vote`, { liked, note }));
  const idle = wb.samples.filter((s) => !s.busy).length;
  const act = async (fn: () => Promise<any>, ok?: string) => {
    setBusy(true); setMsg(null);
    try { const r = await fn(); if (ok) setMsg({ tone: "ok", text: ok }); reload(); onChanged(); return r; }
    catch (e) { setMsg({ tone: "bad", text: (e as Error).message }); }
    finally { setBusy(false); }
  };
  const trialAll = () => act(async () => {
    const r = await post<any>(`/api/channels/${cid}/trial`, { playbook_id: pb });
    const n = r.runs.filter((x: any) => !x.skipped).length;
    setMsg({ tone: "ok", text: n ? `已排队 ${n} 期试跑（每期相当于一次完整生产）。` : "没有可试跑的样片：都在生产中。" });
  });
  const add = (urls: string[]) => act(async () => {
    for (const u of urls) await post(`/api/channels/${cid}/samples`, { url: u, run: runNow && !!pb, playbook_id: pb });
    setUrl("");
  }, `已加入 ${urls.length} 期样片${runNow && pb ? "，并开始试跑" : ""}。`);

  return (
    <div className="wb">
      {tuning ? (
        <div className="cfg-toolbar">
          <label className="cfg-inline">
            <select value={pb ?? ""} onChange={(e) => setPb(Number(e.target.value))} aria-label="试跑用规程">
              {wb.playbooks.map((p) => <option key={p.id} value={p.id}>v{p.version}{p.note ? ` · ${p.note}` : ""} · {PB[p.status]?.label || p.status}{p.id === wb.draft_id ? "（最新）" : ""}</option>)}
            </select>
          </label>
          <button className="btn solid sm" disabled={!pb || !idle || busy} onClick={trialAll}>用 v{chosen?.version ?? "?"} 试跑全部样片{idle ? `（${idle}）` : ""}</button>
        </div>
      ) : null}
      {msg && <Note tone={msg.tone === "bad" ? "bad" : "ok"}>{msg.text}</Note>}

      {wb.summary.length > 0 && (
        <section className="cfg-sec first">
          <div className="cfg-sec-row"><h3 className="cfg-sec-h">偏好汇总</h3></div>
          <div className="tbl-wrap">
            <table className="tbl tight">
              <thead><tr><th>版本</th><th>说明</th><th className="r">已完成</th><th className="r">偏好</th><th className="r">有备注</th></tr></thead>
              <tbody>
                {wb.summary.map((x) => (
                  <tr key={x.playbook_id}><td><b className="num">v{x.version}</b></td><td>{x.note || <span className="muted">—</span>}</td>
                    <td className="r num">{x.samples} 期</td><td className="r num">{x.liked ? <b>{x.liked}</b> : 0}</td><td className="r num">{x.notes}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {wb.queue.length > 0 && (
        <section className="cfg-sec first">
          <div className="cfg-sec-row">
            <h3 className="cfg-sec-h">排队中的试跑 {wb.queue.length} 次</h3>
            {tuning && <button className="btn xs ghost grow-r" onClick={() => { if (confirm("取消所有还没开始的试跑？")) act(() => post(`/api/channels/${cid}/trial-queue/clear`)); }}>清空排队</button>}
          </div>
          <div className="tbl-wrap">
            <table className="tbl tight">
              <thead><tr><th style={{ width: "3em" }}>#</th><th>样片</th><th>版本</th></tr></thead>
              <tbody>
                {wb.queue.map((q, i) => (
                  <tr key={q.id}><td className="num muted">{i + 1}</td><td>{q.title || q.source_id}</td>
                    <td><b className="num">v{q.version}</b>{vnote(q.playbook_id) && <span className="muted"> · {vnote(q.playbook_id)}</span>}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {wb.samples.length === 0 ? (
        <Empty>还没有样片</Empty>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl wb-tbl">
            <thead>
              <tr><th>版本</th><th>状态</th><th className="r">单元 · 必学</th><th className="r">片段</th><th className="r">校注</th><th className="r">字数</th><th className="r">操作</th></tr>
            </thead>
            {wb.samples.map((s) => {
              const complete = s.runs.filter(done);
              const open = cmp?.sid === s.id;
              return (
                <tbody key={s.id} className="wb-grp">
                  <tr className="grp">
                    <td colSpan={7}>
                      <div className="grp-row">
                        <a className="grp-title" href={s.url} target="_blank" rel="noreferrer">{s.title}</a>
                        <span className="muted num">{[day(s.published_at), s.duration ? mins(s.duration) : null].filter(Boolean).join(" · ")}</span>
                        {s.delivery === "REGISTERED" && s.notion_url && <a className="chip ok" href={s.notion_url} target="_blank" rel="noreferrer">库里已有 ↗</a>}
                        {s.stale && <Chip tone="warn" title="采用中的版本不是用最新草稿跑的">采用版本较旧</Chip>}
                        <span className="grp-acts">
                          {complete.length >= 2 && (
                            <button className={`btn xs${open ? " on" : ""}`} aria-expanded={open}
                              onClick={() => setCmp(open ? null : { sid: s.id, a: complete[1].id, b: complete[0].id })}>对比版本</button>
                          )}
                          {tuning && !s.busy && (
                            <button className="btn xs ghost" onClick={() => act(() => post(`/api/channels/${cid}/samples/${s.id}/remove`))}>移出</button>
                          )}
                        </span>
                      </div>
                    </td>
                  </tr>
                  {s.runs.length === 0 && <tr><td colSpan={7} className="muted">还没有试跑。</td></tr>}
                  {s.runs.map((r) => (
                    <tr key={r.id} className={r.id === s.current_run_id ? "cur" : ""}>
                      <td>
                        <b className="num">v{r.version ?? "?"}</b>{r.id === s.current_run_id && <span className="chip ink sm">采用中</span>}
                        {vnote(r.playbook_id) && <div className="wb-vname">{vnote(r.playbook_id)}</div>}
                        {r.note && noteFor !== r.id && <div className="wb-vnote">备注：{r.note}</div>}
                      </td>
                      <td><RunState r={r} /> <span className="muted when">{ago(r.finished_at || r.created_at)}</span></td>
                      <td className="r num">{done(r) ? `${r.units ?? "—"} · ${r.must}` : "—"}</td>
                      <td className="r num">{done(r) ? r.clips : "—"}</td>
                      <td className="r num">{done(r) ? r.notes : "—"}</td>
                      <td className="r num">{done(r) ? r.chars.toLocaleString() : "—"}</td>
                      <td className="r acts">
                        {done(r) && <a className="btn xs ghost" {...linkProps(`/issue/${encodeURIComponent(s.id)}?run=${encodeURIComponent(r.id)}`)}>对照视图</a>}
                        {done(r) && r.id !== s.current_run_id && tuning && (
                          <button className="btn xs" onClick={() => act(() => post(`/api/sources/${s.id}/adopt`, { run_id: r.id }), `已采用 v${r.version}。`)}>采用</button>
                        )}
                        {done(r) && (
                          <button className={`btn xs${r.liked ? " on" : ""}`} aria-pressed={r.liked} title="标为这一期你偏好的版本（可以多选）"
                            onClick={() => vote(r, !r.liked, r.note)}>{r.liked ? "★ 偏好" : "☆ 偏好"}</button>
                        )}
                        {done(r) && <button className="btn xs ghost" onClick={() => { setNoteFor(noteFor === r.id ? null : r.id); setNoteText(r.note || ""); }}>备注</button>}
                        {live(r) && <span className="muted">生产中</span>}
                      </td>
                    </tr>
                  )).flatMap((row, i) => {
                    const r = s.runs[i];
                    return noteFor === r.id ? [row, (
                      <tr key={`${r.id}-note`} className="note-row">
                        <td colSpan={7}>
                          <div className="cfg-toolbar tight">
                            <input className="cfg-input grow" autoFocus value={noteText} onChange={(e) => setNoteText(e.target.value)} placeholder={`v${r.version} 这一版哪里好、哪里不好`}
                              onKeyDown={(e) => { if (e.key === "Enter") { vote(r, r.liked, noteText); setNoteFor(null); } }} aria-label="备注" />
                            <button className="btn xs solid" onClick={() => { vote(r, r.liked, noteText); setNoteFor(null); }}>保存备注</button>
                            <button className="btn xs ghost" onClick={() => setNoteFor(null)}>取消</button>
                          </div>
                        </td>
                      </tr>
                    )] : [row];
                  })}
                  {open && cmp && (
                    <tr className="cmp-row">
                      <td colSpan={7}>
                        {complete.length > 2 && (
                          <div className="cfg-toolbar tight">
                            <label className="cfg-inline"><span>旧</span>
                              <select value={cmp.a} onChange={(e) => setCmp({ ...cmp, a: e.target.value })}>
                                {complete.map((r) => <option key={r.id} value={r.id}>v{r.version} · {ago(r.finished_at || r.created_at)}</option>)}
                              </select></label>
                            <label className="cfg-inline"><span>新</span>
                              <select value={cmp.b} onChange={(e) => setCmp({ ...cmp, b: e.target.value })}>
                                {complete.map((r) => <option key={r.id} value={r.id}>v{r.version} · {ago(r.finished_at || r.created_at)}</option>)}
                              </select></label>
                          </div>
                        )}
                        <Compare a={cmp.a} b={cmp.b} onClose={() => setCmp(null)} />
                      </td>
                    </tr>
                  )}
                </tbody>
              );
            })}
          </table>
        </div>
      )}

      {tuning && (
        <section className="cfg-sec">
          <h3 className="cfg-sec-h">添加样片</h3>
          <div className="cfg-toolbar">
            <input className="cfg-input grow" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="粘贴这个频道的视频链接"
              aria-label="样片链接" onKeyDown={(e) => e.key === "Enter" && url.trim() && add([url.trim()])} />
            <button className="btn sm" disabled={busy || !url.trim()} onClick={() => add([url.trim()])}>加为样片</button>
            <label className="chk"><input type="checkbox" checked={runNow} onChange={(e) => setRunNow(e.target.checked)} />加入后立即用 v{chosen?.version ?? "?"} 试跑</label>
          </div>
          {wb.channel.youtube_channel_id && <Candidates cid={cid} busy={busy} onAdd={(ids) => add(ids.map((id) => `https://www.youtube.com/watch?v=${id}`))} />}
        </section>
      )}
    </div>
  );
}
