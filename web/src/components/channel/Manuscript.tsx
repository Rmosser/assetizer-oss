import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLeaving, usePoll } from "../../hooks";
import { smooth } from "../../util";
import { Icon, Seg } from "./bits";
import { draftOf, type Mode } from "./Header";
import { keys, light } from "./lit";
import type { Annotations, Channel } from "./types";

const PHASE: Record<string, string> = { understanding: "理解", merge: "合并", knowledge: "知识", article: "文章", selection: "选片" };
const DISP: Record<string, string> = { ORCHESTRATION: "系统承担", REDUNDANT: "与上文重复", IGNORED: "未采用", ARTICLE_STYLE: "文章写法" };

type DiffLine = { kind: "same" | "add" | "del"; text: string };
function diffLines(a: string, b: string): DiffLine[] {
  const x = a.split("\n"), y = b.split("\n");
  const n = x.length, m = y.length;
  const L: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push({ kind: "same", text: x[i] }); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) out.push({ kind: "del", text: x[i++] });
    else out.push({ kind: "add", text: y[j++] });
  }
  while (i < n) out.push({ kind: "del", text: x[i++] });
  while (j < m) out.push({ kind: "add", text: y[j++] });
  return out;
}

function Inline({ text }: { text: string }) {
  return <>{text.split(/(\*\*[^*]+\*\*)/).map((p, i) => (p.startsWith("**") && p.endsWith("**") ? <b key={i}>{p.slice(2, -2)}</b> : <Fragment key={i}>{p}</Fragment>))}</>;
}
const lineKind = (t: string) => (/^#{1,6}\s/.test(t) ? "hd" : /^\s*(\d+[.)、]|[-*+])\s/.test(t) ? "li" : "p");
const lineText = (t: string) => t.replace(/^#{1,6}\s+/, "").replace(/^\s*[-*+]\s+/, "");

type Row = {
  text: string; kind: DiffLine["kind"]; rules: [string, { label: string; example: boolean }][];
  other: { label: string; title: string }[]; issue: string | null; clauseIds: string[];
};

/** 稿纸: the learning goal as the user wrote it, with what the compiler made of each sentence in the margin. */
export function Manuscript({ ch, mode, pbId, setPbId, view, setView, focus, onClose, onRetune, onSave, onRecompile, initialText }: {
  ch: Channel; mode: Mode; pbId: number | null; setPbId: (id: number) => void; view: "manuscript" | "rules";
  setView: (v: "manuscript" | "rules") => void; focus: string | null; onClose: () => void; onRetune: () => void;
  onSave: (text: string, note: string) => Promise<void>; onRecompile: (playbookId: number) => Promise<void>; initialText?: string | null;
}) {
  const pbs = [...ch.playbooks].sort((a, b) => a.version - b.version);
  const draftId = draftOf(ch)?.id;
  const pb = pbs.find((p) => p.id === pbId) || pbs[pbs.length - 1] || null;
  const { data: ann } = usePoll<Annotations>(pb ? `/api/playbooks/${pb.id}/annotations` : null, 0);
  const text = ann?.prompt?.text ?? "";
  const prev = pb ? pbs.filter((p) => p.version < pb.version).pop() : null;
  const prevText = prev?.prompt_id ? ch.prompts.find((p) => p.id === prev.prompt_id)?.text ?? null : null;
  const latestPrompt = ch.prompts[0]?.text || "";
  const [diff, setDiff] = useState(true);
  const [edit, setEdit] = useState<{ text: string; note: string } | null>(initialText ? { text: initialText, note: "" } : null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const body = useRef<HTMLDivElement>(null);
  const readOnly = mode === "locked";
  const dock = useLeaving(edit);
  const compiling = ch.compile?.state === "running";
  useEffect(() => { if (initialText) setEdit({ text: initialText, note: "" }); }, [initialText]);
  useEffect(() => {
    if (!focus || !ann) return;
    const t = window.setTimeout(() => {
      light(focus);
      body.current?.querySelector(`[data-k~="${CSS.escape(focus)}"]`)?.scrollIntoView({ block: "center", behavior: smooth() });
    }, 60);
    return () => clearTimeout(t);
  }, [focus, ann, view]);

  const rows = useMemo<Row[]>(() => {
    if (!ann || !text) return [];
    const conflicts = new Map(ann.conflicts.map((c) => [c.clause_id, c.issue]));
    const ruleOf = new Map(ann.rules.map((r) => [r.id, r]));
    let off = 0;
    const cur: Row[] = text.split("\n").map((t) => {
      const start = off, end = off + t.length;
      off = end + 1;
      const cls = ann.clauses.filter((c) => c.start != null && c.start >= start && c.start <= Math.max(end, start));
      const byRule = new Map<string, { label: string; example: boolean }>();
      const other = new Map<string, { label: string; title: string }>();
      let issue: string | null = null;
      for (const c of cls) {
        if (conflicts.has(c.id)) issue = conflicts.get(c.id) || null;
        if ((c.disposition === "RULE" || c.disposition === "EXAMPLE") && c.rule_id) {
          const g = byRule.get(c.rule_id) || { label: ruleOf.get(c.rule_id)?.label || c.rule_id, example: true };
          g.example = g.example && c.disposition === "EXAMPLE";
          byRule.set(c.rule_id, g);
        } else if (c.disposition && DISP[c.disposition]) other.set(c.disposition, { label: DISP[c.disposition], title: c.reason || "" });
      }
      return { text: t, kind: "same", rules: [...byRule.entries()], other: [...other.values()], issue, clauseIds: cls.map((c) => c.id) };
    });
    if (!diff || prevText == null) return cur;
    const out: Row[] = [];
    let k = 0;
    for (const d of diffLines(prevText, text)) {
      if (d.kind === "del") out.push({ text: d.text, kind: "del", rules: [], other: [], issue: null, clauseIds: [] });
      else out.push({ ...cur[k++], kind: d.kind });
    }
    return out;
  }, [ann, text, diff, prevText]);

  const save = async () => {
    if (!edit) return;
    setBusy(true); setErr(null);
    try { await onSave(edit.text, edit.note); setEdit(null); } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  const conflictRules = new Set((ann?.conflicts || []).map((c) => c.rule_id));
  const added = new Set((ann?.vs_previous?.added || []).map((r) => r.id));
  const changed = new Set((ann?.vs_previous?.changed || []).map((x) => x.to.id));
  const cov = ann?.coverage;
  // an imported version has no sentence-by-sentence provenance, an older one no short names: compiling the goal again fixes both
  const again = ann && pb && (text || latestPrompt) && (!ann.has_provenance || !ann.has_labels) ? (
    <button className="ch-lk" disabled={compiling || busy} onClick={async () => {
      setBusy(true); setErr(null);
      try { await onRecompile(pb.id); } catch (e) { setErr((e as Error).message); }
      setBusy(false);
    }}>{compiling ? "编译中…" : "重新编译出新草稿"}</button>
  ) : null;
  return (
    <div className="ch-msh">
      <div className="msrail">
        {pbs.map((p) => (
          <button key={p.id} className={`vt${p.id === pb?.id ? " on" : ""}${p.id === draftId ? " dr" : ""}`}
            title={p.status === "ACTIVE" ? "生效中" : p.id === draftId ? "草稿" : ""} onClick={() => setPbId(p.id)}>v{p.version}</button>
        ))}
        <span className="grow">
          {compiling && <span className="compiling"><span className="ch-prog" style={{ width: 90 }}><b style={{ width: "38%" }} /></span>新版本编译中 · 约 3 分钟</span>}
          <Seg small label="稿纸视图" value={view} onChange={setView} options={[{ value: "manuscript", label: "原文" }, { value: "rules", label: "规则" }]} />
          {view === "manuscript" && prevText != null && !edit && (
            <button className="chk" onClick={() => setDiff(!diff)} aria-pressed={diff}><span className={`bx${diff ? " on" : ""}`}><Icon name="check" size={10} /></span>与 v{prev?.version} 的差异</button>
          )}
          {readOnly ? (<><span className="ch-chip sm">只读</span><button className="ch-btn sm ghost" onClick={onRetune}>回到调试期修改</button></>)
            : !edit && <button className="ch-btn sm" onClick={() => setEdit({ text: text || latestPrompt, note: "" })}><Icon name="pen" size={12} />改学习目标</button>}
          <button className="ch-btn sm" onClick={onClose}>回到工作台</button>
        </span>
      </div>
      <div className="msb" ref={body}>
        {edit ? (
          <div className="ms-edit">
            <textarea value={edit.text} onChange={(e) => setEdit({ ...edit, text: e.target.value })} aria-label="学习目标原文" spellCheck={false} />
          </div>
        ) : view === "rules" ? (
          <div className="rv">
            {ann && <div className="cov">{ann.has_provenance && cov ? `原文 ${cov.clauses} 句 · 落实 ${cov.used} · 系统承担 ${cov.system} · 重复 ${cov.redundant} · 未采用 ${cov.ignored}` : "导入的规程，没有逐句对应"}{again && <> · {again}</>}</div>}
            {err && !edit && <p className="ch-err">{err}</p>}
            <div className="rvh">规则</div>
            {(ann?.rules || []).map((r) => (
              <div key={r.id || r.label} className={`rvi lt-bg${conflictRules.has(r.id) ? " cf" : ""}`} data-k={r.id ? `r:${r.id}` : undefined}>
                <div><b>{r.label}</b>
                  {added.has(r.id) && <span className="tg2">新</span>}{changed.has(r.id) && <span className="tg2">改</span>}{conflictRules.has(r.id) && <span className="tg2">与原则冲突</span>}
                  <div className="ph2">{(r.phases || []).map((p) => <span key={p} className="phc">{PHASE[p] || p}</span>)}</div></div>
                <div className="tx">{r.rule}</div>
              </div>
            ))}
            {!!ann?.facets.length && <><div className="rvh">频道字段</div>{ann.facets.map((f) => (
              <div key={f.key} className="rvi"><div><b>{f.label}</b><div className="ph2"><span className="phc">{f.type === "list" ? "列表" : "文本"}</span></div></div><div className="tx">{f.description}</div></div>
            ))}</>}
            {ann?.article_style && <><div className="rvh">文章写法</div><div className="rvi"><div><b>写法</b></div><div className="tx">{ann.article_style}</div></div></>}
          </div>
        ) : !text && ann ? (
          <div className="ms-empty">
            <p>这一版是导入的规程，没有学习目标原文。</p>
            {latestPrompt ? again : !readOnly && <button className="ch-btn sm" onClick={() => setEdit({ text: latestPrompt, note: "" })}>写学习目标</button>}
            {err && <p className="ch-err">{err}</p>}
          </div>
        ) : (
          <>
            {ch.why && <div className="epi"><p><b>题记</b>　{ch.why}</p><div className="mm">{ann && !ann.has_provenance && <span className="mx g">这一版是导入的规程，和原文没有逐句对应</span>}{again}{err && !edit && <span className="mx">{err}</span>}</div></div>}
            {rows.map((r, i) => {
              const k = lineKind(r.text);
              const ruleKeys = r.rules.map(([id]) => `r:${id}`);
              return (
                <div key={i} className={`msr${k === "hd" ? " gap" : ""}`}>
                  <div className={`mst lt-bg ${k}${r.kind === "add" ? " ins" : r.kind === "del" ? " del" : ""}${r.issue ? " cf" : ""}`}
                    data-k={keys(...ruleKeys, ...r.clauseIds.map((c) => `cl:${c}`)) || undefined}>
                    {r.text.trim() ? <Inline text={lineText(r.text)} /> : <span className="blank" />}
                  </div>
                  <div className={`mm${r.rules.length || r.other.length ? " on" : ""}${r.issue ? " cf" : ""}`}>
                    {r.rules.map(([id, g]) => (
                      <button key={id} className={`ml lt-bg${r.issue ? " cf" : ""}`} data-k={`r:${id}`}>{g.label}{g.example ? " · 示例" : ""}</button>
                    ))}
                    {r.other.map((o) => <span key={o.label} className="ml sys" title={o.title}>{o.label}</span>)}
                    {r.issue && <span className="mx">{r.issue}</span>}
                  </div>
                </div>
              );
            })}
          </>
        )}
      </div>
      {dock.shown && createPortal(  // the sticky panes stack above anything fixed inside them
        <div className={`ch-dock${dock.leaving ? " leaving" : ""}`} role="region" aria-label="保存学习目标" inert={dock.leaving}>
          <span className="pulse" aria-hidden />
          <input value={dock.shown.note} onChange={(e) => edit && setEdit({ ...edit, note: e.target.value })} placeholder="这次改了什么" aria-label="改动说明" />
          <button className="ch-btn sm" onClick={() => { setEdit(null); setErr(null); }}>放弃</button>
          <button className="ch-btn sm solid" disabled={busy || dock.shown.text.trim().length < 30} onClick={save}>{busy ? "保存中…" : "保存并编译"}</button>
          {err && <span className="err">{err}</span>}
        </div>, document.body,
      )}
    </div>
  );
}
