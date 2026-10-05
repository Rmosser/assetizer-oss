import { useState } from "react";
import { post } from "../api";
import { typeset, Words } from "../cjk";
import { Empty, Fault, Stale, Unroll } from "../components/States";
import { usePoll } from "../hooks";
import { useRestoreScroll } from "../router";
import { ago } from "../util";

const PHASE: Record<string, string> = { understanding: "理解", merge: "合并", knowledge: "知识", article: "文章", selection: "选片" };
const STATUS: Record<string, string> = { ACTIVE: "生效中", NEEDS_CONFIRMATION: "待你确认", SUPERSEDED: "已替代", FAILED: "编译失败" };
const DISP: Record<string, string> = { RULE: "规则", EXAMPLE: "示例", ORCHESTRATION: "系统承担", ARTICLE_STYLE: "文章写法", REDUNDANT: "重复", IGNORED: "未采用" };

/** "Run-014 Codex fidelity audit" → a sentence a person would write */
function originText(s?: string | null) {
  if (!s) return "";
  if (/fidelity audit/i.test(s)) return "已逐条核对原始目标，确认无遗漏";
  if (/not independently audited/i.test(s)) return "已编译，尚未逐条核对";
  return s.replace(/Run-\d+\s*/g, "").replace(/compile/i, "编译");
}

function Coverage({ audit }: { audit: any }) {
  if (!audit?.clauses) return null;
  const map: Record<string, any> = Object.fromEntries((audit.clause_map || []).map((m: any) => [m.clause_id, m]));
  const grade: Record<string, any> = Object.fromEntries((audit.grades || []).map((g: any) => [g.rule_id, g]));
  const unc = new Set(audit.uncovered || []);
  return (
    <details className="fold" open>
      <summary><i className="plus" aria-hidden /><span>条款覆盖 · {Math.round((audit.coverage ?? 0) * 100)}% 有去处 · 部分保留 {audit.core_partial ?? 0} · 丢失 {audit.core_dropped ?? 0}</span></summary>
      <div className="cov">
        {audit.clauses.map((c: any) => {
          const m = map[c.id];
          const g = m?.target ? grade[m.target] : null;
          const bad = unc.has(c.id) || (m?.disposition === "IGNORED") || g?.status === "DROPPED";
          return (
            <div key={c.id} className={`cov-row ${bad ? "bad" : ""} ${g?.status === "PARTIAL" ? "partial" : ""}`}>
              <span className="num muted">{c.id}</span>
              <span>{typeset(c.text)}</span>
              <span className="disp">{unc.has(c.id) ? "未覆盖" : DISP[m?.disposition] || m?.disposition}
                {m?.target && <span className="num"> → {m.target}</span>}
                {g?.status === "PARTIAL" && <em> · 部分：{typeset(g.missing)}</em>}
                {m?.disposition === "IGNORED" && m?.reason && <em> · {typeset(m.reason)}</em>}
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
  if (!open) return <button className="btn sm editor-open" onClick={() => setOpen(true)}>修改学习目标</button>;
  return (
    <div className="editor">
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={18} aria-label="频道学习目标" />
      <div className="editor-row">
        <button className="btn solid sm" onClick={async () => {
          try { await post(`/api/channels/${c.id}/prompt`, { text }); setMsg("已保存为新版本，正在编译（约 2–4 分钟），完成后在这里确认。"); setOpen(false); setTimeout(reload, 1500); }
          catch (e) { setMsg(String((e as Error).message)); }
        }}>保存并编译</button>
        <button className="btn ghost sm" onClick={() => setOpen(false)}>取消</button>
        <span className="muted">保存后生成新版本规程；确认前，旧版本继续生效。</span>
      </div>
      {msg && <p className="zhu editor-msg">{msg}</p>}
    </div>
  );
}

function Switch({ on, onChange, children }: { on: boolean; onChange: (v: boolean) => void; children: React.ReactNode }) {
  return <button role="switch" aria-checked={on} className={`switch${on ? " on" : ""}`} onClick={() => onChange(!on)}><i aria-hidden />{children}</button>;
}

function Channel({ c, reload }: { c: any; reload: () => void }) {
  const pb = c.playbooks[0];
  const prompt = c.prompts[0];
  const conflicts = pb?.audit?.policy_conflicts || [];
  const audit = pb?.audit || {};
  const confirm = async () => { await post(`/api/playbooks/${pb.id}/confirm`); reload(); };
  return (
    <section className={`channel${pb?.status === "NEEDS_CONFIRMATION" ? " needs-you" : ""}`}>
      <div className="channel-main">
        <h3><Words>{c.name}</Words></h3>
        <div className="mode"><i className={c.mode === "TRACKED" ? "on" : ""} aria-hidden />
          <span>{c.mode === "TRACKED" ? "长期关注" : "发现中"} · {c.sources ? `已收 ${c.sources} 期${c.last_source ? ` · 最近 ${ago(c.last_source)}` : ""}` : "尚未收录"}</span>
        </div>
        {c.why && <p className="why">{typeset(c.why)}</p>}
        {c.mode === "TRACKED" && c.youtube_channel_id && (
          <div className="ingest">
            <Switch on={!!c.ingest?.enabled} onChange={async (v) => { await post(`/api/channels/${c.id}/ingest`, { enabled: v }); reload(); }}>自动收录新视频</Switch>
            <span className="muted">
              {c.ingest?.enabled ? `每 30 分钟检查一次 · 只收录开启后发布、${c.ingest.min_minutes || 10} 分钟以上的视频` : "开启后，新发布的视频会自动付印，不回溯旧视频"}
            </span>
          </div>
        )}
        {pb && (
          <details className="fold">
            <summary><i className="plus" aria-hidden /><span>频道规则与出处 · {pb.rules.length} 条</span></summary>
            <div className="rules">
              {pb.rules.map((r: any) => (
                <div className="rule" key={r.id}>
                  <div className="id"><span className="num">{r.id}</span><span className="ph">{(r.phases || []).map((p: string) => PHASE[p] || p).join(" · ")}</span></div>
                  <div>
                    <p>{typeset(r.rule)}</p>
                    {r.quote && <blockquote>原文：{typeset(r.quote)}</blockquote>}
                  </div>
                </div>
              ))}
            </div>
          </details>
        )}
        {pb?.status === "NEEDS_CONFIRMATION" && <Coverage audit={pb.audit} />}
        {prompt && (
          <details className="fold">
            <summary><i className="plus" aria-hidden /><span>原始学习目标 · v{prompt.version}{prompt.origin ? " · 来自 Notion" : ""}</span></summary>
            <div className="rawprompt">{typeset(prompt.text)}</div>
            {prompt.origin?.startsWith("http") && <p className="muted src-line">来源：<a href={prompt.origin} target="_blank" rel="noreferrer">{prompt.origin}</a></p>}
          </details>
        )}
        {c.mode === "TRACKED" && <PromptEditor c={c} reload={reload} />}
      </div>
      <div className="pb">
        <span className="label">运行规程</span>
        {pb ? (
          <>
            <div className="ver"><span className="num">v{pb.version}</span><small>{STATUS[pb.status] || pb.status}</small></div>
            <div className="stat">
              <div><span className="num">{pb.rules.length}</span> 条编译规则</div>
              {audit.requirements && <div>核心需求 <span className="num">{audit.requirements.length}</span> 项 · 丢失 <span className="num">{audit.core_dropped ?? 0}</span> · 部分 <span className="num">{audit.core_partial ?? 0}</span></div>}
              {(audit.source || pb.origin) && (
                <details className="fold mini"><summary><i className="plus" aria-hidden /><span>编译来源</span></summary><p className="muted">{originText(audit.source || pb.origin)}</p></details>
              )}
            </div>
            {pb.facets?.length > 0 && (
              <div className="facet-chips">{pb.facets.map((f: any) => <span key={f.key} className="tag">{f.label}</span>)}</div>
            )}
            {pb.status === "NEEDS_CONFIRMATION" && (
              <div className="needs">
                <b>需要你确认一次。</b>确认后，这个频道的新视频会自动按这套规程生产。
                {conflicts.map((x: any) => <div key={x.rule_id} className="conflict">· {typeset(x.issue.replace(/Source-only/g, "只依据视频").replace(/Policy/g, "全局政策"))}</div>)}
                <div className="needs-act"><button className="btn solid sm" onClick={confirm}>确认并启用</button></div>
              </div>
            )}
          </>
        ) : <p className="muted">尚无规程</p>}
      </div>
    </section>
  );
}

export default function Channels() {
  const { data, error, loadedAt, reload } = usePoll<any>("/api/channels", 0);
  useRestoreScroll(!!data);
  if (!data) return <main className="page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const tracked = data.channels.filter((c: any) => c.mode === "TRACKED");
  const others = data.channels.filter((c: any) => c.mode !== "TRACKED");
  return (
    <main className="page">
      <Stale error={error} loadedAt={loadedAt} />
      <section className="hero page-hero">
        <h1 className="hero-title">频道</h1>
        <p className="hero-lede">每个长期关注的频道，都有一份你写下的学习目标。系统把它编译成可执行的规程，从第一次理解视频起就按它拆解知识。没有关注的频道使用通用规程。</p>
      </section>
      <div className="rule-title"><h2>长期关注</h2><span className="aside"><span className="num">{tracked.length}</span> 个频道</span></div>
      {tracked.length === 0 ? (
        <Empty title="还没有长期关注的频道">打开任意一期，在刊头点「加入长期关注」，写下你为什么看这个频道。</Empty>
      ) : tracked.map((c: any) => <Channel key={c.id} c={c} reload={reload} />)}
      {others.length > 0 && (
        <>
          <div className="rule-title"><h2>发现中</h2></div>
          {others.map((c: any) => <Channel key={c.id} c={c} reload={reload} />)}
        </>
      )}
      <div className="rule-title"><h2>通用规程</h2></div>
      <section className="channel">
        <div className="channel-main">
          <h3>通用</h3>
          <p className="why">未建立频道专属学习目标时使用：中性、完整、可追溯地整理内容，不加入任何频道偏好。适合第一次看到的频道。</p>
        </div>
        <div className="pb"><span className="label">运行规程</span><div className="ver"><span className="num">v{data.universal?.version ?? 1}</span><small>生效中</small></div></div>
      </section>
    </main>
  );
}
