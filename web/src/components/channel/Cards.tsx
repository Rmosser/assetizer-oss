import { Fragment, useEffect, useRef, useState } from "react";
import { get } from "../../api";
import { Fold } from "../Fold";
import { hm, Icon, minRule, nextScan, Seg, short, Stepper, Switch, TYPE_ICON, useNarrow } from "./bits";
import { draftOf, type Mode } from "./Header";
import { keys } from "./lit";
import { Opt } from "./NotionPane";
import type { Annotations, Channel, Draft, Mapping, NotionRow, NotionView, Playbook, Preview } from "./types";

/** Which YouTube field a value comes from: the key that ties a setting to the source pane. */
export const FIELD_OF_SOURCE: Record<string, string> = {
  video_title: "f:title", channel: "f:channel", published_date: "f:date", published_datetime: "f:date", access: "f:access", video_url: "f:url",
};
export const FIELD_OF_VAR: Record<string, string> = { video_title: "f:title", channel: "f:channel", date: "f:date", date8: "f:date", url: "f:url" };
const SRC: Record<string, string> = {
  fixed: "固定", template: "模板", published_date: "发布日期", published_datetime: "发布时间", access: "自动判断", key_levels: "关键位摘要",
  headline: "本期题眼", video_title: "视频原标题", video_url: "视频链接", channel: "频道名", duration_min: "时长（分钟）", units: "知识单元数",
  clips: "视频片段数", notes: "校注数", run: "Run 编号",
};
const VAR: Record<string, string> = { video_title: "视频原标题", channel: "频道名", date: "发布日期", date8: "发布日期", headline: "本期题眼", url: "视频链接" };
const ACCESS_FORM: [string, string][] = [["video", "视频"], ["live", "直播"]];
const ACCESS_TIER: [string, string][] = [["public", "公开"], ["member", "会员"]];
const ACCESS_KINDS = ACCESS_FORM.flatMap(([f]) => ACCESS_TIER.map(([t]) => `${t}_${f}`));

/** A setting card. On a phone it folds to its header and a one-line summary. `act` ends the header; a phone's header
 *  is the fold button, so there the card leaves it out and the caller places it in the body. */
function Card({ n, title, right, sum, alert, act, cls, children }: {
  n: string; title: string; right?: React.ReactNode; sum?: React.ReactNode; alert?: boolean; act?: React.ReactNode; cls?: string;
  children: React.ReactNode;
}) {
  const narrow = useNarrow();
  const [open, setOpen] = useState(false);
  if (!narrow) {
    return (
      <section className={`ch-card${cls ? ` ${cls}` : ""}`}>
        <div className="ch-cardh"><span className="cn">{n}</span><h3>{title}</h3>{right && <span className="r">{right}</span>}{act}</div>
        {children}
      </section>
    );
  }
  const r = open ? right : sum ?? right;
  return (
    <section className={`ch-card${open ? "" : " fold"}${cls ? ` ${cls}` : ""}`}>
      <button className="ch-cardh" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="cn">{n}</span><h3>{title}</h3>{r && <span className={`r${alert && !open ? " z" : ""}`}>{r}</span>}
        <span className="chv"><Icon name="chev" size={12} /></span>
      </button>
      <Fold open={open}><div className="ch-cardb">{children}</div></Fold>
    </section>
  );
}

// ------------------------------------------------------------------ 壹 学习目标
export function GoalCard({ mode, ch, pb, ann, newerDraft, onOpen, onRetune, onDraft }: {
  mode: Mode; ch: Channel; pb: Playbook | null; ann: Annotations | null; newerDraft: Playbook | null;
  onOpen: (view: "manuscript" | "rules", focus?: string) => void; onRetune: () => void; onDraft: (why: string) => Promise<void>;
}) {
  const [why, setWhy] = useState(ch.why || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const narrow = useNarrow();
  if (mode === "discovery") {
    return (
      <Card cls="ch-goal" n="壹" title="学习目标" right="未建立">
        <textarea className="ch-why" rows={3} value={why} onChange={(e) => setWhy(e.target.value)} placeholder="为什么长期看这个频道？" aria-label="为什么长期看这个频道" />
        {err && <p className="ch-err">{err}</p>}
        <div className="ch-cfoot"><span className="grow" />
          <button className="ch-btn sm ghost" onClick={() => onOpen("manuscript")}>自己写</button>
          <button className="ch-btn sm solid" disabled={busy || why.trim().length < 4} onClick={async () => {
            setBusy(true); setErr(null);
            try { await onDraft(why.trim()); } catch (e) { setErr((e as Error).message); }
            setBusy(false);
          }}>{busy ? "起草中…" : "起草学习目标"}</button>
        </div>
      </Card>
    );
  }
  const rules = ann?.rules.length ?? pb?.rules.length ?? 0;
  const compiling = ch.compile?.state === "running";
  const failed = ch.compile?.state === "failed";
  const ruleLabel = (id?: string) => ann?.rules.find((r) => r.id === id)?.label || "";
  const diff = mode === "tuning" ? ann?.vs_previous : null;
  // the sheet grows out of this card from its top right corner, where 合上稿纸 then sits (design §R3-10)
  const sheet = <button className="ch-btn sm ch-msx" aria-expanded={false} onClick={() => onOpen("manuscript")}>{mode === "locked" ? "查看稿纸" : "打开稿纸"}</button>;
  return (
    <Card cls="ch-goal" n="壹" title="学习目标" right={pb ? `v${pb.version} · ${rules} 条规则` : null} alert={!!ann?.conflicts.length || failed}
      act={!narrow && sheet}
      sum={pb ? `v${pb.version}${pb.id === draftOf(ch)?.id ? " 草稿" : ""}${ann?.conflicts.length ? ` · ${ann.conflicts.length} 处冲突` : ""}` : null}>
      <div className="ch-hls">
        {compiling && <div className="ch-hl"><b className="htg">编译中</b><span className="tx">新版本 · 约 3 分钟</span><span className="ch-prog" style={{ width: 90 }}><b style={{ width: "40%" }} /></span></div>}
        {failed && <div className="ch-hl cf"><b className="htg">失败</b><span className="tx">编译没有完成：{short(ch.compile?.error || "", 60)}</span></div>}
        {diff?.added.map((r) => (
          <button key={`a${r.id || r.label}`} className="ch-hl" onClick={() => onOpen("manuscript", r.id ? `r:${r.id}` : undefined)}>
            <b className="htg">新</b><span className="tx">{r.label}</span><span className="gt">比 v{diff.version}</span>
          </button>
        ))}
        {diff?.changed.map((x) => (
          <button key={`c${x.to.id || x.to.label}`} className="ch-hl" onClick={() => onOpen("manuscript", x.to.id ? `r:${x.to.id}` : undefined)}>
            <b className="htg">改</b><span className="tx">{x.to.label}</span><span className="gt">比 v{diff.version}</span>
          </button>
        ))}
        {ann?.conflicts.map((c) => (
          <button key={c.clause_id} className="ch-hl cf" onClick={() => onOpen("manuscript", `cl:${c.clause_id}`)}>
            <b className="htg">冲突</b><span className="tx">{ruleLabel(c.rule_id) ? `${ruleLabel(c.rule_id)}：` : ""}{short(c.issue.split(/[；;。]/)[0], 26)}</span><span className="gt">看原句 →</span>
          </button>
        ))}
        {mode === "locked" && newerDraft && (
          <div className="ch-hl dr"><b className="htg">草稿</b><span className="tx">v{newerDraft.version} · {newerDraft.rules.length} 条规则</span>
            <span className="acts"><button className="ch-btn sm" onClick={onRetune}>回到调试期试跑</button></span></div>
        )}
      </div>
      <div className="ch-cfoot">
        <button className="ch-lk" onClick={() => onOpen("rules")}>全部 {rules} 条</button>
        {narrow && <><span className="grow" />{sheet}</>}
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ 贰 Notion 标题
function parseTpl(s: string): { tokens: string[]; sep: string; ok: boolean } {
  const parts = s.split(/(\{\w+\})/).filter((x) => x !== "");
  const isVar = (p: string) => /^\{\w+\}$/.test(p);
  const tokens = parts.filter(isVar).map((p) => p.slice(1, -1));
  const lits = parts.filter((p) => !isVar(p));
  const alternating = parts.every((p, i) => (i % 2 === 0) === isVar(p));
  const ok = !s || (alternating && isVar(parts[parts.length - 1]) && lits.every((l) => l === lits[0]));
  return { tokens, sep: lits[0] ?? " ", ok };
}
const SEPS = [{ value: " ", label: "空格" }, { value: "", label: "无" }, { value: "-", label: "-" }, { value: " · ", label: "·" }, { value: "｜", label: "｜" }];

export function TitleCard({ view, cfg, setProp, ctx }: {
  view: NotionView; cfg: Draft; setProp: (pid: string, m: Mapping | null) => void; ctx: Record<string, string>;
}) {
  const row = view.rows.find((r) => r.type === "title");
  if (!row) return null;
  const m = cfg.properties[row.id];
  const tpl = !m ? "" : m.source === "template" ? String(m.value || "") : m.source === "fixed" ? "" : `{${m.source}}`;
  const { tokens, sep, ok } = parseTpl(tpl);
  const set = (t: string[], s: string) => setProp(row.id, t.length ? { name: row.name, type: "title", source: "template", value: t.map((x) => `{${x}}`).join(s) } : null);
  const hasDate = tokens.find((t) => t === "date" || t === "date8");
  const avail = ["video_title", "channel", "date", "headline"].filter((v) => !tokens.includes(v) && !(v === "date" && hasDate));
  return (
    <Card n="贰" title="Notion 标题" sum={ok ? tokens.map((v) => VAR[v] || v).join(" + ") || "不写入" : "模板"}>
      {ok ? (
        <>
          <div className="ch-comp">
            {!tokens.length && <span className="ph0">不写入</span>}
            {tokens.map((v, i) => (
              <span key={`${v}${i}`} className="ch-tkw">
                {i > 0 && sep !== "" && <span className="sepc">{sep === " " ? "␣" : sep.trim()}</span>}
                <span className="tk lt-ring" data-k={keys(`tok:${v}`, FIELD_OF_VAR[v])} data-side="mid" tabIndex={0}>
                  <small>{VAR[v] || v}</small><span>{ctx[v] || ""}</span>
                  <button className="x" aria-label={`移除${VAR[v] || v}`} onClick={() => set(tokens.filter((_, j) => j !== i), sep)}><Icon name="x" size={10} /></button>
                </span>
              </span>
            ))}
          </div>
          {avail.length > 0 && <div className="ch-adds">{avail.map((v) => <button key={v} className="ch-add" onClick={() => set([...tokens, v], sep)}><Icon name="plus" size={10} />{VAR[v]}</button>)}</div>}
          <div className="ch-row2">
            <span>分隔</span><Seg small label="分隔符" value={sep} options={SEPS} onChange={(s) => set(tokens, s)} />
            {hasDate && <><span className="gap">日期</span>
              <Seg small label="日期格式" value={hasDate} options={[{ value: "date", label: ctx.date || "YYYY-MM-DD" }, { value: "date8", label: ctx.date8 || "YYYYMMDD" }]}
                onChange={(d) => set(tokens.map((t) => (t === "date" || t === "date8" ? d : t)), sep)} /></>}
          </div>
        </>
      ) : (
        <TemplateInput value={tpl} vars={view.template_vars} onChange={(v) => setProp(row.id, v ? { name: row.name, type: "title", source: "template", value: v } : null)} />
      )}
    </Card>
  );
}

function TemplateInput({ value, vars, onChange }: { value: string; vars: Record<string, string>; onChange: (v: string) => void }) {
  return (
    <div className="ch-tplin">
      <input value={value} onChange={(e) => onChange(e.target.value)} aria-label="模板" />
      <div className="ch-adds">{Object.keys(vars).map((k) => <button key={k} className="ch-add" onClick={() => onChange(`${value}{${k}}`)}><Icon name="plus" size={10} />{VAR[k] || k}</button>)}</div>
    </div>
  );
}

// ------------------------------------------------------------------ 叁 Notion 属性
function PagePicker({ db, value, titles, onChange }: { db?: string; value: string[]; titles: Record<string, string | null>; onChange: (v: string[], t: Record<string, string>) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ id: string; title: string }[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const search = async () => {
    if (!db) return;
    try { setErr(null); setHits((await get<any>(`/api/notion/pages?database=${db}&q=${encodeURIComponent(q)}`)).pages); } catch (e) { setErr((e as Error).message); }
  };
  return (
    <div className="ch-picker">
      <div className="opts">{value.map((id) => (
        <span key={id} className="ch-pagechip">{titles[id] || id.slice(0, 8)}<button aria-label="移除" onClick={() => onChange(value.filter((x) => x !== id), {})}><Icon name="x" size={10} /></button></span>
      ))}{!value.length && <span className="q">未选择页面</span>}</div>
      <div className="row"><input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="搜索页面标题" aria-label="搜索关联页面" />
        <button className="ch-btn xs" onClick={search}>搜索</button></div>
      {err && <p className="ch-err">{err}</p>}
      {hits && <div className="hits">{!hits.length && <span className="q">没有找到</span>}{hits.map((p) => (
        <button key={p.id} className="ch-btn xs ghost" onClick={() => { onChange(value.includes(p.id) ? value : [...value, p.id], { [p.id]: p.title }); setHits(null); setQ(""); }}>{p.title || "（无标题）"}</button>
      ))}</div>}
    </div>
  );
}

/** A problem message without the property name the row already shows. */
const brief = (s: string) => s.replace(/^「[^」]*」的?/, "");

function summary(r: NotionRow, m: Mapping | undefined, titles: Record<string, string | null>): React.ReactNode {
  if (!m) return <span>不写入</span>;
  if (m.source === "fixed") {
    if (r.type === "select" || r.type === "status") return m.value ? <><Opt name={m.value} color={r.colors[m.value]} /><span>固定</span></> : <span className="need">未设置</span>;
    if (r.type === "relation") return <span>{(m.value || []).map((id: string) => titles[id] || "页面").join("、") || "未选择"}</span>;
    if (r.type === "multi_select") return <span>{(m.value || []).join("、") || "未设置"}</span>;
    return <span>{String(m.value ?? "") || "未设置"}</span>;
  }
  return <span>{SRC[m.source] || m.source}</span>;
}

function initMapping(r: NotionRow, source: string, view: NotionView): Mapping {
  const m: Mapping = { name: r.name, type: r.type, source };
  if (source === "access") m.value = Object.fromEntries(Object.entries(view.access_labels).map(([k, l]) => [k, !r.options || r.options.includes(l) ? l : ""]));
  if (source === "template") m.value = "";
  if (source === "fixed") m.value = r.type === "relation" || r.type === "multi_select" ? [] : r.type === "checkbox" ? false : "";
  return m;
}

/** 视频 / 直播 × 公开 / 会员 as a 2 × 2 of what each kind writes; one row of options edits the picked cell, which is
 *  this episode's kind until another is picked. A property without options (text) takes typed values. */
function AccessMap({ r, m, set, now }: { r: NotionRow; m: Mapping; set: (m: Mapping) => void; now?: string }) {
  const [pick, setPick] = useState<string | null>(null);
  const v: Record<string, string> = m.value || {};
  const at = pick ?? (now && ACCESS_KINDS.includes(now) ? now : ACCESS_KINDS[0]);
  const gone = (o: string) => !!o && !!r.options && !r.options.includes(o);
  const choose = (o: string) => set({ ...m, value: { ...v, [at]: o } });
  return (
    <>
      <div className="amap" role="radiogroup" aria-label={`${r.name} 按类型`}>
        <span />{ACCESS_TIER.map(([t, l]) => <span key={t} className="ax c">{l}</span>)}
        {ACCESS_FORM.map(([f, fl]) => (
          <Fragment key={f}>
            <span className="ax">{fl}</span>
            {ACCESS_TIER.map(([t, tl]) => {
              const k = `${t}_${f}`, cur = v[k] || "";
              return (
                <button key={k} role="radio" aria-checked={at === k} aria-label={`${fl} · ${tl}`} className={`acell${at === k ? " on" : ""}`} onClick={() => setPick(k)}>
                  {cur ? <Opt name={cur} color={gone(cur) ? "gone" : r.colors[cur]} /> : <span className="nil">不写入</span>}
                  {now === k && <span className={`now${gone(cur) ? " z" : ""}`}>本期</span>}
                </button>
              );
            })}
          </Fragment>
        ))}
      </div>
      <div className="apick">{r.options
        ? <div className="opts">{r.options.map((o) => (
          <button key={o} className={`optb${v[at] === o ? " on" : ""}`} onClick={() => choose(o)}><Opt name={o} color={r.colors[o]} /></button>
        ))}</div>
        : <input className="ch-input" value={v[at] || ""} onChange={(e) => choose(e.target.value)} aria-label={`${r.name} 写入的文字`} />}</div>
    </>
  );
}

function Editor({ r, m, set, view, titles, addTitles, ctx }: {
  r: NotionRow; m: Mapping | undefined; set: (m: Mapping | null) => void; view: NotionView;
  titles: Record<string, string | null>; addTitles: (t: Record<string, string>) => void; ctx: Record<string, string>;
}) {
  const sources = r.sources.filter((s) => s !== "template" || ["rich_text", "url"].includes(r.type));
  return (
    <div className="sked">
      <div className="ch-srcs" role="radiogroup" aria-label={`${r.name} 的值来源`}>
        <button role="radio" aria-checked={!m} className={!m ? "on" : ""} onClick={() => set(null)}>不写入</button>
        {sources.map((s) => (
          <button key={s} role="radio" aria-checked={m?.source === s} className={m?.source === s ? "on" : ""}
            onClick={() => set(m?.source === s ? m : initMapping(r, s, view))}>{SRC[s] || s}</button>
        ))}
      </div>
      {m?.source === "fixed" && (r.type === "select" || r.type === "status") && (
        <div className="opts">{(r.options || []).map((o) => (
          <button key={o} className={`optb${m.value === o ? " on" : ""}`} onClick={() => set({ ...m, value: o })}><Opt name={o} color={r.colors[o]} /></button>
        ))}{m.value && !(r.options || []).includes(m.value) && <span className="optb on"><Opt name={m.value} color="gone" /></span>}</div>
      )}
      {m?.source === "fixed" && r.type === "multi_select" && (() => {
        const cur: string[] = m.value || [];
        const toggle = (o: string) => set({ ...m, value: cur.includes(o) ? cur.filter((x) => x !== o) : [...cur, o] });
        return (
          <div className="opts">{(r.options || []).map((o) => (
            <button key={o} className={`optb${cur.includes(o) ? " on" : ""}`} onClick={() => toggle(o)}><Opt name={o} color={r.colors[o]} /></button>
          ))}{cur.filter((o) => !(r.options || []).includes(o)).map((o) => (
            <button key={`gone:${o}`} className="optb on" onClick={() => toggle(o)}><Opt name={o} color="gone" /></button>
          ))}</div>
        );
      })()}
      {m?.source === "fixed" && r.type === "relation" && (
        <PagePicker db={r.relation_db} value={m.value || []} titles={titles} onChange={(v, t) => { addTitles(t); set({ ...m, value: v }); }} />
      )}
      {m?.source === "fixed" && ["rich_text", "url", "number", "date"].includes(r.type) && (
        <input className="ch-input" type={r.type === "number" ? "number" : r.type === "date" ? "date" : "text"} value={m.value ?? ""}
          onChange={(e) => set({ ...m, value: r.type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value })} aria-label={`${r.name} 固定值`} />
      )}
      {m?.source === "fixed" && r.type === "checkbox" && <Switch on={!!m.value} onChange={(v) => set({ ...m, value: v })} label={r.name} />}
      {m?.source === "template" && <TemplateInput value={String(m.value || "")} vars={view.template_vars} onChange={(v) => set({ ...m, value: v })} />}
      {m?.source === "access" && <AccessMap r={r} m={m} set={set} now={ctx.access} />}
      {r.problem && <p className="ch-fix"><span>{brief(r.problem)}</span>{view.database.url && <a className="ch-lk" href={view.database.url} target="_blank" rel="noreferrer">在 Notion 里改 ↗</a>}</p>}
    </div>
  );
}

export function PropsCard({ view, cfg, setProp, preview, titles, addTitles, testCopy, open, setOpen, onRetarget }: {
  view: NotionView; cfg: Draft; setProp: (pid: string, m: Mapping | null) => void; preview: Preview | null;
  titles: Record<string, string | null>; addTitles: (t: Record<string, string>) => void; testCopy: boolean;
  open: string | null; setOpen: (id: string | null) => void; onRetarget: () => void;
}) {
  const ctx = preview?.context || {};
  const probs = preview?.problems || {};
  const warns = preview?.warnings || view.warnings || {};
  const rows = view.rows.filter((r) => r.type !== "title");
  const auto = rows.filter((r) => !r.writable && !r.deleted);
  const loose = rows.filter((r) => r.writable && r.type === "relation" && !cfg.properties[r.id]);
  const main = rows.filter((r) => (r.writable && !loose.includes(r)) || r.deleted);
  const urlRow = rows.find((r) => r.writable && r.type === "url" && !cfg.properties[r.id]);
  const socket = (r: NotionRow) => {
    const m = cfg.properties[r.id];
    const bad = probs[r.id] || (r.deleted ? "Notion 里已没有这个属性" : null);
    const isOpen = open === r.id;
    return (
      <div key={r.id} className={`sk lt-ring${isOpen ? " open" : ""}${bad ? " bad" : ""}`} data-k={keys(`p:${r.id}`, m && FIELD_OF_SOURCE[m.source])} data-side="mid">
        <button className="skr" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : r.id)}>
          <span className="ti"><Icon name={TYPE_ICON[r.type] || "text"} /></span>
          <span className="nm">{r.name}</span>
          <span className={`sv${bad ? " need" : m ? " set" : ""}`}>{bad ? <span>{short(brief(bad), 22)}</span> : summary(r, m, titles)}</span>
          <span className="chv"><Icon name="chev" size={12} /></span>
        </button>
        <Fold open={isOpen}>{r.deleted
          ? <div className="sked"><button className="ch-btn sm" onClick={() => setProp(r.id, null)}>移除这个映射</button></div>
          : <Editor r={{ ...r, problem: bad }} m={m} set={(nm) => setProp(r.id, nm)} view={view} titles={titles} addTitles={addTitles} ctx={ctx} />}</Fold>
      </div>
    );
  };
  const bads = main.filter((r) => probs[r.id] || r.deleted).length;
  return (
    <Card n="叁" title="Notion 属性" alert={bads > 0}
      sum={bads ? `${bads} 处对不上` : `${testCopy ? "测试副本 · " : ""}${main.filter((r) => cfg.properties[r.id]).length} 项`}>
      <div className="ch-dbh"><Icon name="db" /><b>{view.database.title || "Notion 数据库"}</b>{testCopy && !view.database.title.includes("测试副本") && <span className="ch-tag">测试副本</span>}
        <span className="grow" /><button className="ch-btn sm ghost" onClick={onRetarget}>{testCopy ? "换成正式库" : "换库"}</button></div>
      {warns.video_url && (
        <p className="ch-warn"><span>{warns.video_url}</span>
          {urlRow && <button className="ch-btn xs" onClick={() => setProp(urlRow.id, { name: urlRow.name, type: urlRow.type, source: "video_url" })}>用「{urlRow.name}」</button>}</p>
      )}
      {main.map(socket)}
      {auto.length > 0 && (
        <div className={`sk${open === "_auto" ? " open" : ""}`}>
          <button className="skr" aria-expanded={open === "_auto"} onClick={() => setOpen(open === "_auto" ? null : "_auto")}>
            <span className="ti"><Icon name="time" /></span><span className="nm">Notion 自动维护</span><span className="sv">{auto.length} 项</span><span className="chv"><Icon name="chev" size={12} /></span>
          </button>
          <Fold open={open === "_auto"}><div className="sked">{auto.map((r) => <div key={r.id} className="gi"><span className="gn2">{r.name}</span><span className="gs">Notion 自动填写</span></div>)}</div></Fold>
        </div>
      )}
      {loose.length > 0 && (
        <div className={`sk${open === "_loose" ? " open" : ""}`}>
          <button className="skr" aria-expanded={open === "_loose"} onClick={() => setOpen(open === "_loose" ? null : "_loose")}>
            <span className="ti"><Icon name="relation" /></span><span className="nm">其他关联</span><span className="sv">{loose.length} 项不写入</span><span className="chv"><Icon name="chev" size={12} /></span>
          </button>
          <Fold open={open === "_loose"}><div className="sked">{loose.map((r) => (
            <div key={r.id} className="gi"><span className="gn2">{r.name}</span><span className="gs">不写入</span>
              <button className="ch-btn xs ghost" onClick={() => { setProp(r.id, initMapping(r, "fixed", view)); setOpen(r.id); }}>固定页面</button></div>
          ))}</div></Fold>
        </div>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ 肆 页面结构
const SKEL: Record<string, string[]> = { chapters: ["v", "d", "h", "", "s", "q", "h"], "reading-guide": ["s", "h1", "q", "h", "v", "", "t"], default: ["c", "h", "v", "", "s", "h", "v"] };
const TPL_LABEL: Record<string, string> = { chapters: "章节 + 原话参考", "reading-guide": "读图学习导航", default: "通用" };

export function StructureCard({ view, cfg, update, ctx }: { view: NotionView; cfg: Draft; update: (p: Partial<Draft>) => void; ctx: Record<string, string> }) {
  const [editing, setEditing] = useState(false);
  const parts = (cfg.heading || "").split(/(\{\w+\})/).filter(Boolean);
  const emoji = !["none", "parent"].includes(cfg.icon);
  return (
    <Card n="肆" title="页面结构" sum={TPL_LABEL[cfg.template] || view.templates[cfg.template]}>
      <div className="ch-tpls" role="radiogroup" aria-label="正文模板">
        {Object.keys(view.templates).map((k) => (
          <button key={k} role="radio" aria-checked={cfg.template === k} className={`tpl${cfg.template === k ? " on lt-ring" : ""}`} data-k="tpl" data-side="mid"
            onClick={() => update({ template: k })}>
            <span className="skel">{(SKEL[k] || SKEL.default).map((c, i) => <i key={i} className={c} />)}</span>
            <span>{TPL_LABEL[k] || view.templates[k]}</span>
          </button>
        ))}
      </div>
      {cfg.template !== "default" && (
        <div className="ch-fld top"><span>页面大标题</span>
          {editing ? (
            <div><TemplateInput value={cfg.heading} vars={view.template_vars} onChange={(v) => update({ heading: v })} />
              <button className="ch-btn xs" onClick={() => setEditing(false)}>完成</button></div>
          ) : (
            <button className="ch-comp click lt-ring" data-k="tpl:h1" data-side="mid" onClick={() => setEditing(true)} aria-label="编辑页面大标题">
              {!parts.length && <span className="ph0">不加</span>}
              {parts.map((p, i) => {
                const v = /^\{(\w+)\}$/.exec(p)?.[1];
                return v ? <span key={i} className="tk"><small>{VAR[v] || v}</small><span>{ctx[v] || ""}</span></span>
                  : <span key={i} className="tk tx"><small>文字</small><span>{p}</span></span>;
              })}
            </button>
          )}
        </div>
      )}
      <div className="ch-fld"><span>页面图标</span>
        <span className="row">
          <Seg small label="页面图标" value={emoji ? "emoji" : cfg.icon} onChange={(v) => update({ icon: v === "emoji" ? "📘" : v })}
            options={[{ value: "parent", label: "上级页面的" }, { value: "none", label: "不设" }, { value: "emoji", label: "Emoji" }]} />
          {emoji && <input className="ch-input emoji" value={cfg.icon} onChange={(e) => update({ icon: e.target.value || "none" })} aria-label="Emoji" />}
        </span>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ 伍 收录
export function IntakeCard({ ch, mode, onSet, onBoard }: {
  ch: Channel; mode: Mode; onSet: (enabled: boolean, min: number) => Promise<void>; onBoard?: () => void;
}) {
  const ing = ch.ingest || {};
  const [min, setMin] = useState(ing.min_minutes ?? 10);
  const [err, setErr] = useState<string | null>(null);
  const t = useRef<number | undefined>(undefined);
  useEffect(() => { setMin(ing.min_minutes ?? 10); }, [ing.min_minutes]);
  const can = mode === "locked" && ch.mode === "TRACKED" && !!ch.youtube_channel_id;
  const send = (en: boolean, m: number) => onSet(en, m).catch((e) => setErr((e as Error).message));
  const since = ing.since ? new Date(ing.since) : null;
  if (onBoard) {  // a locked channel's intake is run from its 看板: here only where it stands
    const state = ing.enabled ? `每 ${ing.every_minutes || 30} 分钟` : "已暂停";
    return (
      <Card n="伍" title="收录" right={state} sum={state}>
        <p className="ch-row2 q">{ing.enabled ? "自动收录中" : "自动收录已暂停"} · {minRule(ing.min_minutes ?? 10)}</p>
        <button className="ch-btn sm" onClick={onBoard}>去看板</button>
      </Card>
    );
  }
  return (
    <Card n="伍" title="收录" right={mode === "locked" ? `每 ${ing.every_minutes || 30} 分钟` : "定稿后开启"}
      sum={mode !== "locked" ? "定稿后开启" : can && ing.enabled ? `每 ${ing.every_minutes || 30} 分钟` : "已暂停"}>
      <div className="ch-sw"><Switch on={!!ing.enabled && can} disabled={!can} label="自动收录新视频" onChange={(v) => send(v, min)} /><span>自动收录新视频</span></div>
      <div className="ch-row2">
        <span>{min ? "短于" : "时长"}</span>
        <Stepper label="最短时长" zero="不限" value={min} onChange={(v) => { setMin(v); clearTimeout(t.current); t.current = window.setTimeout(() => send(!!ing.enabled, v), 600); }} />
        {min > 0 && <span>分钟的不收</span>}
      </div>
      {can && ing.enabled && (
        <p className="ch-row2 q">{ing.last_poll ? `上次 ${hm(ing.last_poll)}${nextScan(ing) ? ` · ${nextScan(ing)}` : ""}` : "等第一次查看"}
          {since && !Number.isNaN(+since) ? ` · 自 ${since.getMonth() + 1} 月 ${since.getDate()} 日起` : ""}</p>
      )}
      {err && <p className="ch-err">{err}</p>}
    </Card>
  );
}
