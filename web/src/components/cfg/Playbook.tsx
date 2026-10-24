import { useEffect, useMemo, useState } from "react";
import { post } from "../../api";
import { Chip, Empty, Note, PB, Seg, stamp } from "./ui";

const PHASE: Record<string, string> = { understanding: "理解", merge: "合并", knowledge: "知识", article: "文章", selection: "选片" };
const DISP: Record<string, string> = { RULE: "规则", EXAMPLE: "示例", ORCHESTRATION: "系统承担", ARTICLE_STYLE: "文章写法", REDUNDANT: "重复", IGNORED: "未采用" };
const GRADE: Record<string, { label: string; tone: "ok" | "warn" | "bad" }> = {
  PRESERVED: { label: "完整保留", tone: "ok" }, PARTIAL: { label: "部分保留", tone: "warn" }, DROPPED: { label: "丢失", tone: "bad" },
};
type View = "rules" | "facets" | "style" | "coverage";

function originText(s?: string | null) {
  if (!s) return "—";
  if (/^compiler:p(\d+)/.test(s)) return `学习目标 v${s.match(/^compiler:p(\d+)/)![1]} 编译`;
  if (/^seed:/.test(s)) return `导入（${s.replace(/^seed:/, "")}）`;
  return s;
}

function Rules({ p }: { p: any }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const rows = p.rules.filter((r: any) => !q || `${r.id} ${r.rule} ${r.quote || ""}`.includes(q));
  const flip = (id: string) => setOpen((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  return (
    <>
      <div className="cfg-toolbar tight">
        <input className="cfg-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="筛选规则" aria-label="筛选规则" />
        {q && <span className="muted">{rows.length} / {p.rules.length} 条</span>}
        <button className="btn xs ghost" onClick={() => setOpen(open.size ? new Set() : new Set(rows.map((r: any) => r.id)))}>{open.size ? "收起全部原文" : "展开全部原文"}</button>
      </div>
      <div className="tbl-wrap">
        <table className="tbl">
          <thead><tr><th style={{ width: "7.5em" }}>用于</th><th>规则</th></tr></thead>
          <tbody>
            {rows.map((r: any) => (
              <tr key={r.id} title={r.id}>
                <td className="muted">{(r.phases || []).map((x: string) => PHASE[x] || x).join(" · ")}</td>
                <td>
                  {r.rule}
                  {r.quote && (
                    <>
                      <button className="btn xs ghost inline" aria-expanded={open.has(r.id)} onClick={() => flip(r.id)}>{open.has(r.id) ? "收起原文" : "原文"}</button>
                      {open.has(r.id) && <blockquote className="cfg-quote">{r.quote}</blockquote>}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Coverage({ audit }: { audit: any }) {
  const [only, setOnly] = useState(true);
  const map: Record<string, any> = Object.fromEntries((audit.clause_map || []).map((m: any) => [m.clause_id, m]));
  const grade: Record<string, any> = Object.fromEntries((audit.grades || []).map((g: any) => [g.rule_id, g]));
  const unc = new Set(audit.uncovered || []);
  const rows = (audit.clauses || []).map((c: any) => {
    const m = map[c.id];
    const g = m?.target ? grade[m.target] : null;
    const bad = unc.has(c.id) || m?.disposition === "IGNORED" || g?.status === "DROPPED" || g?.status === "PARTIAL";
    return { c, m, g, bad };
  });
  const shown = only ? rows.filter((x: any) => x.bad) : rows;
  return (
    <>
      <div className="cfg-toolbar tight">
        <span>{Math.round((audit.coverage ?? 0) * 100)}% 的条款有去处 · 部分保留 {audit.core_partial ?? 0} · 丢失 {audit.core_dropped ?? 0}</span>
        <label className="chk"><input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} />只看有问题的（{rows.filter((x: any) => x.bad).length}）</label>
      </div>
      {(audit.policy_conflicts || []).length > 0 && (
        <Note tone="warn">{audit.policy_conflicts.length} 处与「只依据视频」原则冲突，以原则为准：{audit.policy_conflicts.map((x: any) => x.issue).join("；")}</Note>
      )}
      {shown.length === 0 ? <Empty>没有问题条款。</Empty> : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead><tr><th style={{ width: "4em" }}>条款</th><th>原文</th><th style={{ width: "13em" }}>去处</th></tr></thead>
            <tbody>
              {shown.map(({ c, m, g, bad }: any) => (
                <tr key={c.id} className={bad ? "bad" : ""}>
                  <td className="mono">{c.id}</td>
                  <td>{c.text}</td>
                  <td>
                    {unc.has(c.id) ? <Chip tone="bad">未覆盖</Chip> : <span>{DISP[m?.disposition] || m?.disposition || "—"}{m?.target && <span className="mono muted"> → {m.target}</span>}</span>}
                    {g && GRADE[g.status] && <div><Chip tone={GRADE[g.status].tone}>{GRADE[g.status].label}</Chip></div>}
                    {g?.missing && <div className="cfg-hint">缺：{g.missing}</div>}
                    {m?.disposition === "IGNORED" && m?.reason && <div className="cfg-hint">{m.reason}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/** 规程: every compiled version of the channel's playbook, and what one version contains. */
export default function PlaybookPanel({ channel, playbooks, reload }: { channel: any | null; playbooks: any[]; reload: () => void }) {
  const pref = useMemo(() => (playbooks.find((p) => p.status === "NEEDS_CONFIRMATION") || playbooks.find((p) => p.status === "ACTIVE") || playbooks[0])?.id, [playbooks]);
  const [sel, setSel] = useState<number | null>(pref ?? null);
  const [view, setView] = useState<View>("rules");
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => { if (!playbooks.some((p) => p.id === sel)) setSel(pref ?? null); }, [playbooks, pref, sel]);
  if (!playbooks.length) return <Empty>还没有规程。到「学习目标」写下这个频道要学什么，保存后会编译出第一版。</Empty>;
  const p = playbooks.find((x) => x.id === sel) || playbooks[0];
  const audit = p.audit || {};
  const confirmVersion = async (x: any) => {
    const locked = channel?.stage === "LOCKED";
    const text = locked
      ? `直接启用规程 v${x.version}？\n这个频道已定稿，启用后之后的每一期都按 v${x.version} 生产，不经过试跑。\n建议先「回到调试期」用样片试跑。`
      : `定稿规程 v${x.version}：频道进入已定稿状态并开启自动收录，样片「采用中」的版本写入 Notion。确定？`;
    if (!confirm(text)) return;
    try { await post(`/api/playbooks/${x.id}/confirm`); setMsg(null); reload(); } catch (e) { setMsg((e as Error).message); }
  };
  return (
    <div className="pbp">
      {channel && (
        <div className="tbl-wrap">
          <table className="tbl pbp-versions">
            <thead><tr><th>版本</th><th>状态</th><th>来源</th><th>编译于</th><th className="r">规则</th><th className="r">条款覆盖</th><th className="r">部分 · 丢失</th><th className="r">操作</th></tr></thead>
            <tbody>
              {playbooks.map((x) => {
                const a = x.audit || {};
                return (
                  <tr key={x.id} className={`sel-row${x.id === p.id ? " on" : ""}`} onClick={() => setSel(x.id)}>
                    <td><b className="num">v{x.version}</b></td>
                    <td><Chip tone={PB[x.status]?.tone}>{PB[x.status]?.label || x.status}</Chip></td>
                    <td className="muted">{originText(x.origin)}</td>
                    <td className="num muted">{stamp(x.created_at)}</td>
                    <td className="r num">{x.rules.length}</td>
                    <td className="r num">{a.coverage != null ? `${Math.round(a.coverage * 100)}%` : "—"}</td>
                    <td className="r num">{a.grades ? `${a.core_partial ?? 0} · ${a.core_dropped ?? 0}` : "—"}</td>
                    <td className="r acts">
                      {x.status === "NEEDS_CONFIRMATION" && (
                        <button className="btn xs" onClick={(e) => { e.stopPropagation(); confirmVersion(x); }}>{channel.stage === "LOCKED" ? "直接启用" : "定稿"}</button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {msg && <Note tone="bad">{msg}</Note>}
      <section className="cfg-sec">
        <div className="cfg-sec-row">
          <h3 className="cfg-sec-h">v{p.version} 的内容</h3>
          <Seg<View> label="查看" value={view} onChange={setView} options={[
            { value: "rules", label: `规则 ${p.rules.length}` },
            { value: "facets", label: `频道字段 ${p.facets.length}` },
            { value: "style", label: "文章写法" },
            ...(audit.clauses ? [{ value: "coverage" as View, label: `条款覆盖 ${Math.round((audit.coverage ?? 0) * 100)}%` }] : []),
          ]} />
        </div>
        {view === "rules" && (p.rules.length ? <Rules p={p} /> : <Empty>没有规则。</Empty>)}
        {view === "facets" && (p.facets.length ? (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead><tr><th style={{ width: "12em" }}>字段</th><th style={{ width: "8em" }}>名称</th><th style={{ width: "5em" }}>类型</th><th>说明</th></tr></thead>
              <tbody>{p.facets.map((f: any) => <tr key={f.key}><td className="mono">{f.key}</td><td>{f.label}</td><td className="muted">{f.type === "list" ? "列表" : "文本"}</td><td>{f.description}</td></tr>)}</tbody>
            </table>
          </div>
        ) : <Empty>没有频道专属字段：知识单元只用通用字段。</Empty>)}
        {view === "style" && (p.article_style ? <p className="cfg-prose">{p.article_style}</p> : <Empty>没有专门的文章写法要求。</Empty>)}
        {view === "coverage" && audit.clauses && <Coverage audit={audit} />}
      </section>
    </div>
  );
}
