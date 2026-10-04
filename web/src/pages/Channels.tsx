import { useState } from "react";
import { post } from "../api";
import { usePoll } from "../hooks";
import { ago } from "../util";

const PHASE: Record<string, string> = { understanding: "理解", merge: "合并", knowledge: "知识", article: "文章", selection: "选片" };
const STATUS: Record<string, string> = { ACTIVE: "生效", NEEDS_CONFIRMATION: "待确认", SUPERSEDED: "已替代", FAILED: "失败" };

const DISP: Record<string, string> = { RULE: "规则", EXAMPLE: "示例", ORCHESTRATION: "系统承担", ARTICLE_STYLE: "文章写法", REDUNDANT: "重复", IGNORED: "未采用" };

function Coverage({ audit }: { audit: any }) {
  if (!audit?.clauses) return null;
  const map: Record<string, any> = Object.fromEntries((audit.clause_map || []).map((m: any) => [m.clause_id, m]));
  const grade: Record<string, any> = Object.fromEntries((audit.grades || []).map((g: any) => [g.rule_id, g]));
  const unc = new Set(audit.uncovered || []);
  return (
    <details className="fold" open>
      <summary>条款覆盖矩阵 · {Math.round((audit.coverage ?? 0) * 100)}% 条款有去处 · {audit.core_partial} 条规则部分保留 · {audit.core_dropped} 条丢失</summary>
      <div className="cov">
        {audit.clauses.map((c: any) => {
          const m = map[c.id];
          const g = m?.target ? grade[m.target] : null;
          const bad = unc.has(c.id) || (m?.disposition === "IGNORED") || g?.status === "DROPPED";
          return (
            <div key={c.id} className={`cov-row ${bad ? "bad" : ""} ${g?.status === "PARTIAL" ? "partial" : ""}`}>
              <span className="mono muted">{c.id}</span>
              <span>{c.text}</span>
              <span className="disp">{unc.has(c.id) ? "未覆盖" : DISP[m?.disposition] || m?.disposition}
                {m?.target && <span className="mono"> → {m.target}</span>}
                {g?.status === "PARTIAL" && <em> · 部分：{g.missing}</em>}
                {m?.disposition === "IGNORED" && m?.reason && <em> · {m.reason}</em>}
              </span>
            </div>
          );
        })}
      </div>
    </details>
  );
}

function PromptEditor({ c, reload }: { c: any; reload: () => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState<string>(c.prompts[0]?.text || "");
  const [msg, setMsg] = useState<string | null>(null);
  if (!open) return <button className="btn sm" style={{ marginTop: 18 }} onClick={() => setOpen(true)}>编辑原始 Prompt</button>;
  return (
    <div className="editor">
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={18} aria-label="原始 Prompt" />
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 10 }}>
        <button className="btn solid sm" onClick={async () => {
          try { await post(`/api/channels/${c.id}/prompt`, { text }); setMsg("已保存为新版本，正在编译（约 2–4 分钟），完成后在这里确认。"); setOpen(false); setTimeout(reload, 1500); }
          catch (e) { setMsg(String((e as Error).message)); }
        }}>保存并编译</button>
        <button className="btn ghost sm" onClick={() => setOpen(false)}>取消</button>
        <span className="muted" style={{ fontSize: 12.5 }}>保存后生成新 Playbook 版本，确认前旧版本继续生效。</span>
      </div>
      {msg && <p className="zhu" style={{ fontSize: 13 }}>{msg}</p>}
    </div>
  );
}

function Channel({ c, reload }: { c: any; reload: () => void }) {
  const pb = c.playbooks[0];
  const prompt = c.prompts[0];
  const conflicts = pb?.audit?.policy_conflicts || [];
  const audit = pb?.audit || {};
  const confirm = async () => { await post(`/api/playbooks/${pb.id}/confirm`); reload(); };
  return (
    <section className="channel">
      <div>
        <h3>{c.name}</h3>
        <div className="mode"><i style={{ background: c.mode === "TRACKED" ? "var(--ink)" : "var(--ink-3)" }} />
          {c.mode === "TRACKED" ? "长期关注" : "发现中"} · 已收 {c.sources} 期{c.last_source ? ` · 最近 ${ago(c.last_source)}` : ""}
        </div>
        {c.why && <p className="why">{c.why}」</p>}
        {c.mode === "TRACKED" && c.youtube_channel_id && (
          <div style={{ marginTop: 18, display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
            <label className={`switch ${c.ingest?.enabled ? "on" : ""}`}
              onClick={async () => { await post(`/api/channels/${c.id}/ingest`, { enabled: !c.ingest?.enabled }); reload(); }}>
              <i /> 自动收录新视频
            </label>
            <span className="muted" style={{ fontSize: 12.5 }}>
              {c.ingest?.enabled ? `每 30 分钟检查一次 · 只收录开启后发布、${c.ingest.min_minutes || 10} 分钟以上的视频` : "开启后，新发布的视频会自动付印（不回溯旧视频）"}
            </span>
          </div>
        )}
        {pb && (
          <details className="fold">
            <summary>频道规则与溯源（{pb.rules.length} 条）</summary>
            <div className="rules">
              {pb.rules.map((r: any) => (
                <div className="rule" key={r.id}>
                  <div className="id">{r.id}<span className="ph">{(r.phases || []).map((p: string) => PHASE[p] || p).join(" · ")}</span></div>
                  <div>
                    <p>{r.rule}</p>
                    {r.quote && <blockquote>原文：{r.quote}</blockquote>}
                  </div>
                </div>
              ))}
            </div>
          </details>
        )}
        {pb?.status === "NEEDS_CONFIRMATION" && <Coverage audit={pb.audit} />}
        {prompt && (
          <details className="fold">
            <summary>用户原始 Prompt · v{prompt.version}{prompt.origin ? " · 来自 Notion" : ""}</summary>
            <div className="rawprompt">{prompt.text}</div>
            {prompt.origin?.startsWith("http") && <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>来源：<a href={prompt.origin} target="_blank" rel="noreferrer" style={{ borderBottom: "1px solid var(--rule-2)" }}>{prompt.origin}</a></p>}
          </details>
        )}
        {c.mode === "TRACKED" && <PromptEditor c={c} reload={reload} />}
      </div>
      <div className="pb">
        <span className="caps">Runtime Playbook</span>
        {pb ? (
          <>
            <div className="ver" style={{ marginTop: 10 }}>v{pb.version}<small>{STATUS[pb.status] || pb.status}</small></div>
            <div className="stat">
              <div><b>{pb.rules.length}</b> 条编译规则</div>
              {audit.requirements && <div>核心需求 <b>{audit.requirements.length}</b> 项 · 丢失 <b>{audit.core_dropped ?? 0}</b> · 部分 <b>{audit.core_partial ?? 0}</b></div>}
              <div className="muted" style={{ fontSize: 13 }}>{audit.source || pb.origin}</div>
            </div>
            {pb.facets?.length > 0 && (
              <div className="facet-chips">{pb.facets.map((f: any) => <span key={f.key}>{f.label}</span>)}</div>
            )}
            {pb.status === "NEEDS_CONFIRMATION" && (
              <div className="needs">
                <b>需要你确认一次。</b>确认后，这个频道的新视频会自动按这套规则生产。
                {conflicts.map((x: any) => <div key={x.rule_id} style={{ marginTop: 8 }}>· {x.issue}</div>)}
                <div style={{ marginTop: 12 }}><button className="btn solid sm" onClick={confirm}>确认并启用</button></div>
              </div>
            )}
          </>
        ) : <p className="muted">尚无 Playbook</p>}
      </div>
    </section>
  );
}

export default function Channels() {
  const { data, reload } = usePoll<any>("/api/channels", 0);
  if (!data) return <div className="loading">展卷</div>;
  return (
    <main className="page">
      <section className="hero" style={{ gridTemplateColumns: "1fr" }}>
        <div>
          <h1 className="hero-date" style={{ fontSize: "clamp(56px, 7vw, 104px)" }}>频道</h1>
          <p className="hero-lede" style={{ marginTop: 22, maxWidth: "40em" }}>
            每个长期关注的频道都有一份你写下的学习目标（原始 Prompt），系统把它编译成可执行的 Playbook，从第一次理解视频开始就按它来拆解知识。
            未关注的频道使用通用模式（Universal）。
          </p>
        </div>
      </section>
      <div className="rule-title"><h2>长期关注</h2><span className="aside caps">{data.channels.filter((c: any) => c.mode === "TRACKED").length} 个频道</span></div>
      <div className="channel-grid">
        {data.channels.map((c: any) => <Channel key={c.id} c={c} reload={reload} />)}
      </div>
      <div className="rule-title"><h2>通用模式</h2></div>
      <section className="channel">
        <div>
          <h3>Universal</h3>
          <p className="why">未建立频道专属学习目标时使用：中性、完整、可追溯地整理内容，不加入任何频道偏好。适合第一次看到的频道。」</p>
        </div>
        <div className="pb"><span className="caps">Runtime Playbook</span><div className="ver" style={{ marginTop: 10 }}>v{data.universal?.version ?? 1}<small>生效</small></div></div>
      </section>
    </main>
  );
}
