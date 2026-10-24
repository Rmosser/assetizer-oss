import { useEffect, useMemo, useState } from "react";
import { post } from "../../api";
import { Note, Seg, stamp, useDirty } from "./ui";

type Line = { kind: "same" | "add" | "del"; text: string };

/** Line diff (LCS); prompts are a few hundred lines at most. */
function diffLines(a: string, b: string): Line[] {
  const x = a.split("\n"), y = b.split("\n");
  const n = x.length, m = y.length;
  const L: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out: Line[] = [];
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

function Diff({ a, b }: { a: string; b: string }) {
  const lines = useMemo(() => diffLines(a, b), [a, b]);
  const changed = lines.filter((l) => l.kind !== "same").length;
  if (!changed) return <Note>两个版本内容相同。</Note>;
  // show changes with two lines of context
  const keep = new Set<number>();
  lines.forEach((l, i) => { if (l.kind !== "same") for (let k = i - 2; k <= i + 2; k++) keep.add(k); });
  const out: React.ReactNode[] = [];
  let gap = false;
  lines.forEach((l, i) => {
    if (!keep.has(i)) { if (!gap) out.push(<div key={`g${i}`} className="dl gap">⋯</div>); gap = true; return; }
    gap = false;
    out.push(<div key={i} className={`dl ${l.kind}`}><i>{l.kind === "add" ? "+" : l.kind === "del" ? "−" : ""}</i><span>{l.text || " "}</span></div>);
  });
  return <div className="cfg-diff" aria-label="差异">{out}</div>;
}

/** 学习目标: the raw prompt the playbook is compiled from. Saving makes a new version and compiles it. */
export default function PromptPanel({ c, reload }: { c: any; reload: () => void }) {
  const prompts: any[] = c.prompts || [];
  const latest = prompts[0];
  const [baseId, setBaseId] = useState<number | null>(latest?.id ?? null);
  const base = prompts.find((p) => p.id === baseId) || latest;
  const [text, setText] = useState<string>(base?.text || "");
  const [note, setNote] = useState("");
  const [mode, setMode] = useState<"edit" | "diff">("edit");
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const setDirty = useDirty();
  const dirty = !!base && text !== base.text;
  useEffect(() => { setDirty("prompt", dirty); return () => setDirty("prompt", false); }, [dirty, setDirty]);
  useEffect(() => { if (latest && baseId == null) { setBaseId(latest.id); setText(latest.text); } }, [latest, baseId]);

  const pick = (id: number) => {
    if (dirty && !confirm("编辑器里有未保存的修改，切换版本会丢失。确定？")) return;
    const p = prompts.find((x) => x.id === id);
    setBaseId(id); setText(p?.text || ""); setMsg(null);
  };
  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      await post(`/api/channels/${c.id}/prompt`, { text, note: note.trim() || null });
      setMsg({ tone: "ok", text: `已保存为 v${(latest?.version || 0) + 1}，正在编译。` });
      setNote(""); setBaseId(null); reload();
    } catch (e) { setMsg({ tone: "bad", text: (e as Error).message }); }
    setBusy(false);
  };
  const prev = base ? prompts.find((p) => p.version === base.version - 1) : null;

  return (
    <div className="pmt">
      <div className="pmt-grid">
        <aside className="pmt-versions" aria-label="学习目标版本">
          {prompts.length === 0 && <p className="muted">还没有学习目标。</p>}
          {prompts.map((p) => (
            <button key={p.id} className={`pmt-v${p.id === base?.id ? " on" : ""}`} onClick={() => pick(p.id)}>
              <b className="num">v{p.version}</b>
              <span className="muted">{p.origin?.startsWith("http") ? "来自 Notion" : p.origin === "onboarding" ? "建档" : p.origin === "manual" ? "手动修改" : p.origin || "—"}</span>
              <span className="muted num">{stamp(p.created_at)}</span>
              {p.note && <span className="pmt-note">{p.note}</span>}
            </button>
          ))}
        </aside>
        <div className="pmt-main">
          <div className="cfg-toolbar tight">
            <Seg value={mode} onChange={setMode} label="视图" options={[{ value: "edit", label: "编辑" }, { value: "diff", label: dirty ? `改动（相对 v${base?.version}）` : prev ? `与 v${prev.version} 的差异` : "差异" }]} />
            {base?.origin?.startsWith("http") && <a className="muted" href={base.origin} target="_blank" rel="noreferrer">Notion 原文 ↗</a>}
          </div>
          {mode === "edit" ? (
            <textarea className="pmt-text" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} aria-label="学习目标" />
          ) : dirty ? <Diff a={base.text} b={text} /> : prev ? <Diff a={prev.text} b={base.text} /> : <Note>这是第一个版本，没有可对比的旧版本。</Note>}
          <div className="cfg-toolbar">
            <input className="cfg-input grow" value={note} onChange={(e) => setNote(e.target.value)} placeholder="改动说明（可选）" aria-label="改动说明" />
            <button className="btn solid sm" disabled={busy || !dirty || text.trim().length < 30} onClick={save}>保存为 v{(latest?.version || 0) + 1} 并编译</button>
            <button className="btn sm ghost" disabled={!dirty} onClick={() => setText(base?.text || "")}>还原</button>
          </div>
          {base && base.id !== latest?.id && <Note tone="warn">正在基于旧版本 v{base.version} 修改；保存后会成为最新的 v{(latest?.version || 0) + 1}。</Note>}
          {msg && <Note tone={msg.tone}>{msg.text}</Note>}
        </div>
      </div>
    </div>
  );
}
