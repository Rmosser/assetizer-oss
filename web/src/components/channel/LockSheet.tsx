import { useEffect, useId, useRef, useState } from "react";
import { type ChannelBackup, gdrive, get, type Pass, post } from "../../api";
import { smooth } from "../../util";
import { Sheet, type SheetHandle } from "../Sheet";
import { day, Icon, Seg, short, Stepper, Switch } from "./bits";
import type { LockPreview, LockRow, NotionView } from "./types";

type Base = "write" | "keep" | "replace";

const PASS = [{ value: "on", label: "传" }, { value: "off", label: "不传" }];
const passOf = (v: Pass | undefined) => (v == null ? "" : v ? "on" : "off");

/** 网盘备份 of the channel's author, members-only and public videos apart: 传 or 不传, neither lit until it is chosen */
export function BackupSegs({ member, pub, onChange, name }: {
  member: Pass; pub: Pass; onChange: (f: "member" | "public", v: boolean) => void; name: string;
}) {
  return (
    <span className="ch-passes">
      <span className="ch-pass">
        <span className="n">会员</span>
        <Seg small label={`${name}：会员`} value={passOf(member)} options={PASS} onChange={(x) => onChange("member", x === "on")} />
      </span>
      <span className="ch-pass">
        <span className="n">公开</span>
        <Seg small label={`${name}：公开`} value={passOf(pub)} options={PASS} onChange={(x) => onChange("public", x === "on")} />
      </span>
    </span>
  );
}

/** 定稿单: lock on one version and decide, per sample, what goes to Notion — then the 「定」 stamp. */
export function LockSheet({ cid, defaultPb, onClose, onLocked, onConflict, onRetarget, onTrial }: {
  cid: string; defaultPb: number; onClose: () => void; onLocked: (r: { version: number; write: number; keep: number; wait: number; rerun?: number; ingest: any; backfill?: number }) => void;
  onConflict: (clauseId: string) => void; onRetarget: () => void; onTrial: (pbId: number) => Promise<void>;
}) {
  const [pbId, setPbId] = useState(defaultPb);
  const [lp, setLp] = useState<LockPreview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [base, setBase] = useState<Record<string, Base>>({});
  const [wait, setWait] = useState<Record<string, boolean>>({});
  const [rerun, setRerun] = useState<Set<string>>(new Set());
  const [ing, setIng] = useState<{ enabled: boolean; min: number } | null>(null);
  const [skip, setSkip] = useState<Set<string>>(new Set());  // posted while tuned, unticked: not taken in
  const [busy, setBusy] = useState(false);
  const [stamped, setStamped] = useState(false);
  // 网盘备份: undefined while it is asked, null when there is no drive (or no YouTube ID); what the author has, else unchosen
  const [bk, setBk] = useState<ChannelBackup | null | undefined>(undefined);
  const [pass, setPass] = useState<{ member: Pass; public: Pass }>({ member: null, public: null });
  const sheet = useRef<SheetHandle>(null);
  const close = (then?: () => void) => (sheet.current ? sheet.current.close(then) : (then || onClose)());
  useEffect(() => {
    let alive = true;
    gdrive.channel(cid).then((b) => {
      if (!alive) return;
      setBk(b.ready ? b : null);
      if (b.ready) setPass({ member: b.member ?? null, public: b.public ?? null });
    }, () => { if (alive) setBk(null); });  // not to be asked: 定稿 goes as it did
    return () => { alive = false; };
  }, [cid]);
  const backup = bk ? (pass.member != null && pass.public != null ? { member: pass.member, public: pass.public } : null) : undefined;
  // the line is far down a long sheet: beside 定稿, why it waits, and a way there
  const bkRow = useRef<HTMLDivElement>(null);
  const why = useId();
  const toBackup = () => {
    const row = bkRow.current;
    if (!row) return;
    row.scrollIntoView({ block: "center", behavior: smooth() });
    row.querySelectorAll(".ch-seg")[pass.member == null ? 0 : 1]?.querySelector<HTMLButtonElement>("button")?.focus({ preventScroll: true });
  };
  useEffect(() => {
    let alive = true;
    setLp(null); setErr(null); setRerun(new Set()); setWait({});  // chosen for that version
    get<LockPreview>(`/api/channels/${cid}/lock-preview?playbook_id=${pbId}`)
      .then((d) => { if (!alive) return; setLp(d); setIng((x) => x || { enabled: true, min: d.ingest.min_minutes ?? 10 }); })
      .catch((e) => alive && setErr((e as Error).message));
    return () => { alive = false; };
  }, [cid, pbId]);
  const baseOf = (r: LockRow): Base => base[r.source_id] || (r.library ? "keep" : "write");
  const canWait = (r: LockRow) => !!r.running && !r.adopted?.pinned && baseOf(r) !== "keep";
  const v = lp?.playbook.version;
  // pins stay, a video has one active run at a time, and a page Assetizer did not write is only ever kept
  const canRerun = (r: LockRow) => !!r.adopted && !r.adopted.pinned && r.adopted.version !== v && !r.running && (!r.library || r.can_replace);
  const reruns = (r: LockRow) => rerun.has(r.source_id) && canRerun(r);
  const action = (r: LockRow) => (reruns(r) ? "rerun" : baseOf(r) === "keep" ? "keep" : canWait(r) && wait[r.source_id] ? "wait" : baseOf(r));
  const setRerunOf = (r: LockRow, on: boolean) => setRerun((x) => { const n = new Set(x); if (on) n.add(r.source_id); else n.delete(r.source_id); return n; });
  const lock = async () => {
    if (!lp) return;
    setBusy(true); setErr(null);
    try {
      const r = await post<any>(`/api/channels/${cid}/lock`, {
        playbook_id: pbId, samples: lp.samples.filter((s) => s.adopted || s.running).map((s) => ({ source_id: s.source_id, action: action(s) })),
        ingest: lp.ingest.available && ing ? { enabled: ing.enabled, min_minutes: ing.min } : null,
        backfill: (lp.missed || []).filter((m) => !skip.has(m.id)).map((m) => ({ id: m.id, title: m.title || "", duration: m.duration ?? null })),
        ...(backup ? { backup } : {}),
      });
      setStamped(true);
      window.setTimeout(() => close(() => onLocked(r)), 700);
    } catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  // 定稿 writes what each sample adopts, whichever version made it: what is going in that this version did not make
  const written = (r: LockRow) => { const a = action(r); return a === "keep" ? null : a === "rerun" ? v ?? null : a === "wait" || !r.adopted ? r.running?.version ?? null : r.adopted.version; };
  const samples = lp?.samples || [];
  // what is not on this version first; by what each one has, so a row does not jump while it is being chosen for
  const rows = [...samples].sort((a, b) => Number(a.adopted?.version === v) - Number(b.adopted?.version === v));
  const others = samples.map(written).filter((x): x is number => x != null && x !== v);
  const tally = [...new Set(others)].sort((a, b) => b - a).map((x) => `v${x} × ${others.filter((o) => o === x).length}`).join("、");
  const toRun = samples.filter((r) => canRerun(r) && baseOf(r) !== "keep");
  // this version is under way on the video already: waiting for it is the same, without a second run
  const toWait = samples.filter((r) => r.adopted && r.adopted.version !== v && canWait(r) && r.running!.version === v);
  const allOn = toRun.every(reruns) && toWait.every((r) => wait[r.source_id]);
  const nRerun = samples.filter(reruns).length;
  const toggleAll = () => {
    setRerun(allOn ? new Set() : new Set([...rerun, ...toRun.map((r) => r.source_id)]));
    setWait({ ...wait, ...Object.fromEntries(toWait.map((r) => [r.source_id, !allOn])) });
  };
  const pinnedOff = (r: LockRow) => r.adopted?.pinned && r.adopted.version !== v && <span className="n">钉住</span>;
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
            {(!lp.tested || others.length > 0 || nRerun > 0 || lp.conflicts.length > 0 || !lp.target.configured || lp.target.test_copy || !lp.library_checked) && (
            <div className="sgrp">
              <div className="gh">留意</div>
              {!lp.tested && (
                <div className="srow cf"><span className="t">v{v} 还没在样片上跑过</span>
                  <button className="ch-btn sm" onClick={async () => { try { await onTrial(pbId); close(); } catch (e) { setErr((e as Error).message); } }}>去试跑</button></div>
              )}
              {(others.length > 0 || nRerun > 0) && (
                <div className="srow tally"><span className="t">{others.length > 0 ? `${others.length} 期不是 v${v}：${tally}` : `${nRerun} 期按 v${v} 重跑后写入`}</span>
                  {lp.tested && toRun.length + toWait.length + nRerun > 0 && (
                    <button className={`ch-btn sm${allOn ? " ghost" : ""}`} aria-pressed={allOn} onClick={toggleAll}>{allOn ? "取消重跑" : `按 v${v} 重跑后写入`}</button>
                  )}
                </div>
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
            )}
            <div className="sgrp">
              <div className="gh">写入 Notion</div>
              {rows.map((r) => (
                <div key={r.source_id}>
                  <div className="srow">
                    <span className="t" title={r.title}>{r.title}</span>
                    {!r.adopted && !r.running ? <span className="n z">还没有完成的版本</span>
                      : r.library && !r.can_replace ? <span className="n" title="库里那一页不是课代表写的">库里已有 · 保留</span>
                      : r.library ? (<><span className="n">库里已有</span>
                        <Seg<string> small label={`${r.title}：库里已有`} value={reruns(r) ? "rerun" : baseOf(r)}
                          onChange={(x) => { setRerunOf(r, x === "rerun"); if (x !== "rerun") setBase({ ...base, [r.source_id]: x as Base }); }}
                          options={[{ value: "keep", label: "保留库里的页" }, { value: "replace", label: `换成 v${r.adopted?.version ?? "?"}`, title: "原页逐块更新，你写的、改过的、评论过的留着" },
                            ...(canRerun(r) ? [{ value: "rerun", label: `换成 v${v} 重跑`, title: `v${v} 失败就换成 v${r.adopted!.version}` }] : [])]} />{pinnedOff(r)}</>)
                      : r.adopted && canRerun(r) ? (<><span className="n">将写入</span>
                        <Seg small label={`${r.title}：将写入`} value={reruns(r) ? "rerun" : "now"} onChange={(x) => setRerunOf(r, x === "rerun")}
                          options={[{ value: "now", label: `v${r.adopted.version}` }, { value: "rerun", label: `v${v} 重跑`, title: `v${v} 失败就写 v${r.adopted.version}` }]} /></>)
                      : r.adopted ? (<><span className="n">将写入</span><span className="v">v{written(r)}</span>{pinnedOff(r)}<span className="ok"><Icon name="check" size={10} /></span></>)
                      : <span className="n">等 v{r.running?.version} 跑完</span>}
                  </div>
                  {r.adopted && canWait(r) && (
                    <div className="srow sub">
                      <span className="t">v{r.running!.version} 还在跑</span>
                      <Seg small label={`${r.title}：还在跑`} value={wait[r.source_id] ? "wait" : "now"} onChange={(x) => setWait({ ...wait, [r.source_id]: x === "wait" })}
                        options={[{ value: "now", label: `现在写 v${r.adopted.version}` }, { value: "wait", label: `等 v${r.running!.version} 跑完`, title: `v${r.running!.version} 失败就写 v${r.adopted.version}` }]} />
                    </div>
                  )}
                  {r.queued.some((q) => !reruns(r) || q !== v) && <div className="srow sub quiet"><span className="t">v{r.queued.filter((q) => !reruns(r) || q !== v).join("、v")} 还在排队，定稿后取消</span></div>}
                </div>
              ))}
            </div>
            {(lp.missed?.length || lp.missed_error) ? (
              <div className="sgrp">
                <div className="gh">调试期间发的 · 定稿后补录</div>
                {lp.missed?.map((m) => (
                  <label key={m.id} className="srow">
                    <input type="checkbox" checked={!skip.has(m.id)} aria-label={`补录 ${m.title || m.id}`}
                      onChange={() => setSkip((x) => { const n = new Set(x); if (n.has(m.id)) n.delete(m.id); else n.add(m.id); return n; })} />
                    <span className="t" title={m.title || m.id}>{m.title || m.id}</span>
                    <span className="n">{day(m.published)}</span>
                  </label>
                ))}
                {lp.missed_error && <div className="srow quiet"><span className="t">{short(lp.missed_error, 40)}</span></div>}
              </div>
            ) : null}
            {((lp.ingest.available && ing) || bk) && (
              <div className="sgrp">
                <div className="gh">之后</div>
                {lp.ingest.available && ing && (
                  <div className="srow">
                    <Switch on={ing.enabled} onChange={(x) => setIng({ ...ing, enabled: x })} label="自动收录新视频" />
                    <span className="t">自动收录新视频</span>
                    <span className="n">{ing.min ? "短于" : "时长"}</span><Stepper label="最短时长" zero="不限" value={ing.min} onChange={(x) => setIng({ ...ing, min: x })} />
                    <span className="n">{ing.min ? "分钟的不收 · " : "· "}只收定稿之后发布的</span>
                  </div>
                )}
                {bk && (
                  <div className="srow" ref={bkRow}>
                    <span className="t">网盘备份</span>
                    <BackupSegs name="网盘备份" member={pass.member} pub={pass.public} onChange={(f, v) => setPass({ ...pass, [f]: v })} />
                  </div>
                )}
              </div>
            )}
          </>
        )}
        {err && <p className="ch-err" role="alert">{err}</p>}
        <div className="sfoot">
          {lp && backup === null && <button type="button" id={why} className="why" onClick={toBackup}>先选网盘备份</button>}
          <button className="ch-btn" onClick={() => close()}>取消</button>
          <button className="ch-btn solid" disabled={!lp || !lp.tested || busy || backup === null || bk === undefined} onClick={lock}
            aria-describedby={lp && backup === null ? why : undefined}>{stamped ? "已定稿" : `定稿 v${v ?? ""}${nRerun ? ` · 重跑 ${nRerun} 期` : ""}`}</button>
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
