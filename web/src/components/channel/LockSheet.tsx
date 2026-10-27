import { useEffect, useRef, useState } from "react";
import { get, post } from "../../api";
import { Sheet, type SheetHandle } from "../Sheet";
import { Icon, Seg, short, Stepper, Switch } from "./bits";
import type { LockPreview, LockRow, NotionView } from "./types";

type Base = "write" | "keep" | "replace";

/** 定稿单: lock on one version and decide, per sample, what goes to Notion — then the 「定」 stamp. */
export function LockSheet({ cid, defaultPb, onClose, onLocked, onConflict, onRetarget, onTrial }: {
  cid: string; defaultPb: number; onClose: () => void; onLocked: (r: { version: number; write: number; keep: number; wait: number; ingest: any }) => void;
  onConflict: (clauseId: string) => void; onRetarget: () => void; onTrial: (pbId: number) => Promise<void>;
}) {
  const [pbId, setPbId] = useState(defaultPb);
  const [lp, setLp] = useState<LockPreview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [base, setBase] = useState<Record<string, Base>>({});
  const [wait, setWait] = useState<Record<string, boolean>>({});
  const [ing, setIng] = useState<{ enabled: boolean; min: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [stamped, setStamped] = useState(false);
  const sheet = useRef<SheetHandle>(null);
  const close = (then?: () => void) => (sheet.current ? sheet.current.close(then) : (then || onClose)());
  useEffect(() => {
    let alive = true;
    setLp(null); setErr(null);
    get<LockPreview>(`/api/channels/${cid}/lock-preview?playbook_id=${pbId}`)
      .then((d) => { if (!alive) return; setLp(d); setIng((x) => x || { enabled: true, min: d.ingest.min_minutes || 10 }); })
      .catch((e) => alive && setErr((e as Error).message));
    return () => { alive = false; };
  }, [cid, pbId]);
  const baseOf = (r: LockRow): Base => base[r.source_id] || (r.library ? "keep" : "write");
  const canWait = (r: LockRow) => !!r.running && !r.adopted?.pinned && baseOf(r) !== "keep";
  const action = (r: LockRow) => (baseOf(r) === "keep" ? "keep" : canWait(r) && wait[r.source_id] ? "wait" : baseOf(r));
  const lock = async () => {
    if (!lp) return;
    setBusy(true); setErr(null);
    try {
      const r = await post<any>(`/api/channels/${cid}/lock`, {
        playbook_id: pbId, samples: lp.samples.filter((s) => s.adopted || s.running).map((s) => ({ source_id: s.source_id, action: action(s) })),
        ingest: lp.ingest.available && ing ? { enabled: ing.enabled, min_minutes: ing.min } : null,
      });
      setStamped(true);
      window.setTimeout(() => close(() => onLocked(r)), 700);
    } catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  const v = lp?.playbook.version;
  return (
    <Sheet ref={sheet} onClose={onClose}>
      <div className="ch-sheet">
        <div className="sh">
          <h2>定稿</h2>
          {lp && lp.candidates.length > 1 && (
            <Seg small label="定稿版本" value={String(pbId)} onChange={(x) => setPbId(Number(x))}
              options={lp.candidates.map((c) => ({ value: String(c.id), label: `v${c.version}${c.status === "ACTIVE" ? " 生效中" : c.id === lp.candidates[0].id ? " 草稿" : ""}` }))} />
          )}
          <span className={`stamp${stamped ? " hit" : ""}`} aria-hidden>定</span>
        </div>
        {!lp && !err && <p className="q">正在核对样片和 Notion……</p>}
        {lp && (
          <>
            <div className="sgrp">
              <div className="gh">写入 Notion</div>
              {lp.samples.map((r) => (
                <div key={r.source_id}>
                  <div className="srow">
                    <span className="t" title={r.title}>{r.title}</span>
                    {!r.adopted && !r.running ? <span className="n z">还没有完成的版本</span>
                      : r.library && !r.can_replace ? <span className="n" title="库里那一页不是资产刊写的">库里已有 · 保留</span>
                      : r.library ? (<><span className="n">库里已有</span>
                        <Seg small label={`${r.title}：库里已有`} value={baseOf(r)} onChange={(x) => setBase({ ...base, [r.source_id]: x })}
                          options={[{ value: "keep", label: "保留库里的页" }, { value: "replace", label: `换成 v${r.adopted?.version ?? "?"}`, title: "旧页移到 Notion 归档，不删除" }]} /></>)
                      : r.adopted ? (<><span className="n">将写入</span><span className="v">v{r.adopted.version}</span><span className="ok"><Icon name="check" size={10} /></span></>)
                      : <span className="n">等 v{r.running?.version} 跑完</span>}
                  </div>
                  {r.adopted && canWait(r) && (
                    <div className="srow sub">
                      <span className="t">v{r.running!.version} 还在跑</span>
                      <Seg small label={`${r.title}：还在跑`} value={wait[r.source_id] ? "wait" : "now"} onChange={(x) => setWait({ ...wait, [r.source_id]: x === "wait" })}
                        options={[{ value: "now", label: `现在写 v${r.adopted.version}` }, { value: "wait", label: `等 v${r.running!.version} 跑完`, title: `v${r.running!.version} 失败就写 v${r.adopted.version}` }]} />
                    </div>
                  )}
                  {r.queued.length > 0 && <div className="srow sub quiet"><span className="t">v{r.queued.join("、v")} 还在排队，定稿后取消</span></div>}
                </div>
              ))}
            </div>
            <div className="sgrp">
              <div className="gh">留意</div>
              {!lp.tested && (
                <div className="srow cf"><span className="t">v{v} 还没在样片上跑过</span>
                  <button className="ch-btn sm" onClick={async () => { try { await onTrial(pbId); close(); } catch (e) { setErr((e as Error).message); } }}>去试跑</button></div>
              )}
              {lp.conflicts.length > 0 && (
                <button className="srow cf" onClick={() => close(() => onConflict(lp.conflicts[0].clause_id))}>
                  <span className="t">v{v} 有 {lp.conflicts.length} 处与「只依据视频」冲突，执行时按原则</span><span className="n z">看原句 →</span>
                </button>
              )}
              {!lp.target.configured ? (
                <div className="srow cf"><span className="t">还没有 Notion 目标</span><button className="ch-btn sm" onClick={() => close(onRetarget)}>选库</button></div>
              ) : lp.target.test_copy ? (
                <div className="srow"><span className="t">写入「{lp.target.title}」</span><button className="ch-btn sm ghost" onClick={() => close(onRetarget)}>换成正式库</button></div>
              ) : null}
              {!lp.library_checked && <div className="srow quiet"><span className="t">没能核对库里已有的页：{short(lp.library_error || "", 40)}</span></div>}
            </div>
            {lp.ingest.available && ing && (
              <div className="sgrp">
                <div className="gh">之后</div>
                <div className="srow">
                  <Switch on={ing.enabled} onChange={(x) => setIng({ ...ing, enabled: x })} label="自动收录新视频" />
                  <span className="t">自动收录新视频</span>
                  <span className="n">短于</span><Stepper label="最短时长" value={ing.min} onChange={(x) => setIng({ ...ing, min: x })} /><span className="n">分钟的不收 · 只收定稿之后发布的</span>
                </div>
              </div>
            )}
          </>
        )}
        {err && <p className="ch-err" role="alert">{err}</p>}
        <div className="sfoot">
          <button className="ch-btn" onClick={() => close()}>取消</button>
          <button className="ch-btn solid" disabled={!lp || !lp.tested || busy} onClick={lock}>{stamped ? "已定稿" : `定稿 v${v ?? ""}`}</button>
        </div>
      </div>
    </Sheet>
  );
}

/** 换库: point the channel at another Notion database without the command line. */
export function TargetPicker({ cid, current, onClose, onDone }: {
  cid: string; current: string | null; onClose: () => void; onDone: (v: NotionView) => void;
}) {
  const [q, setQ] = useState("");
  const [list, setList] = useState<{ id: string; title: string; url: string }[] | null>(null);
  const [pick, setPick] = useState<string | null>(null);
  const [test, setTest] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const sheet = useRef<SheetHandle>(null);
  const close = (then?: () => void) => (sheet.current ? sheet.current.close(then) : (then || onClose)());
  const search = async (s: string) => {
    try { setErr(null); setList((await get<any>(`/api/notion/databases?q=${encodeURIComponent(s)}`)).databases); } catch (e) { setErr((e as Error).message); }
  };
  useEffect(() => { search(""); }, []);
  const go = async () => {
    if (!pick) return;
    setBusy(true); setErr(null);
    try { const v = await post<NotionView>(`/api/channels/${cid}/notion/target`, { database: pick, is_test_copy: test }); close(() => onDone(v)); }
    catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  const norm = (x?: string | null) => (x || "").replace(/-/g, "");
  return (
    <Sheet ref={sheet} onClose={onClose} title="写入哪个 Notion 数据库">
      <div className="ch-target">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search(q)} placeholder="搜索数据库，或粘贴数据库链接" aria-label="搜索数据库" />
        <div className="list">
          {list === null && !err && <span className="q">读取中……</span>}
          {list?.length === 0 && <span className="q">没有找到。integration 需要先被加到数据库的 Connections 里。</span>}
          {list?.map((d) => (
            <button key={d.id} className={`it${pick === d.id ? " on" : ""}`} onClick={() => { setPick(d.id); setTest(/测试副本/.test(d.title)); }}>
              <Icon name="db" /><span>{d.title || "未命名数据库"}</span>{norm(d.id) === norm(current) && <span className="ch-chip sm">当前</span>}
            </button>
          ))}
          {/^https?:\/\/|[0-9a-f]{32}/i.test(q) && <button className={`it${pick === q ? " on" : ""}`} onClick={() => setPick(q)}><Icon name="ext" /><span>用这个链接：{short(q, 40)}</span></button>}
        </div>
        <label className="chk"><input type="checkbox" checked={test} onChange={(e) => setTest(e.target.checked)} />这是测试副本</label>
        {err && <p className="ch-err" role="alert">{err}</p>}
        <div className="sfoot">
          <button className="ch-btn" onClick={() => close()}>取消</button>
          <button className="ch-btn solid" disabled={!pick || busy} onClick={go}>{busy ? "切换中…" : "换成这个库"}</button>
        </div>
      </div>
    </Sheet>
  );
}
