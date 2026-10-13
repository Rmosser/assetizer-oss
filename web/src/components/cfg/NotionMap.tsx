import { useEffect, useState } from "react";
import { get, post } from "../../api";
import { Chip, Field, Note, useDirty } from "./ui";

type Row = {
  id: string; name: string; type: string; options?: string[]; relation_db?: string; writable: boolean;
  sources: string[]; mapping: any | null; problem: string | null; sample: any; deleted?: boolean;
};
type View = {
  configured: boolean; database: { id: string; url: string; title: string }; config: { template: string; heading: string; icon: string };
  rows: Row[]; problems: number; page_titles: Record<string, string | null>; sample_source: { id: string; title: string } | null;
  templates: Record<string, string>; source_labels: Record<string, string>; template_vars: Record<string, string>; access_labels: Record<string, string>;
};
type Cfg = { template: string; heading: string; icon: string };

const TYPE: Record<string, string> = {
  title: "标题", rich_text: "文本", select: "单选", multi_select: "多选", status: "状态", date: "日期", number: "数字",
  checkbox: "勾选", url: "链接", relation: "关联", created_time: "创建时间", last_edited_time: "编辑时间", formula: "公式",
  rollup: "汇总", people: "人员", files: "文件", created_by: "创建者", last_edited_by: "编辑者",
};

function sampleText(v: any, titles: Record<string, string | null>): string {
  if (v === null || v === undefined || v === "") return "（空）";
  if (Array.isArray(v)) return v.map((x) => titles[x] ?? x).join("、");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function PagePicker({ db, value, titles, onChange }: { db?: string; value: string[]; titles: Record<string, string | null>; onChange: (v: string[], t: Record<string, string>) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<any[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const search = async () => {
    if (!db) return;
    try { setErr(null); setHits((await get<any>(`/api/notion/pages?database=${db}&q=${encodeURIComponent(q)}`)).pages); }
    catch (e) { setErr((e as Error).message); }
  };
  return (
    <div className="nm-picker">
      <div className="nm-chips">
        {value.map((id) => (
          <span key={id} className="chip ink">{titles[id] || id.slice(0, 8)}
            <button className="nm-x" aria-label="移除" onClick={() => onChange(value.filter((x) => x !== id), {})}>×</button></span>
        ))}
        {!value.length && <span className="muted">未选择页面</span>}
      </div>
      <div className="cfg-inline">
        <input className="cfg-input" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="搜索页面标题" aria-label="搜索关联页面" />
        <button className="btn xs" onClick={search}>搜索</button>
      </div>
      {err && <Note tone="bad">{err}</Note>}
      {hits && (
        <div className="nm-hits">
          {hits.length === 0 && <span className="muted">没有找到</span>}
          {hits.map((p) => (
            <button key={p.id} className="btn xs ghost" onClick={() => { onChange(value.includes(p.id) ? value : [...value, p.id], { [p.id]: p.title }); setHits(null); setQ(""); }}>{p.title || "（无标题）"}</button>
          ))}
        </div>
      )}
    </div>
  );
}

function ValueEditor({ r, m, set, view, titles, addTitles }: { r: Row; m: any; set: (m: any) => void; view: View; titles: Record<string, string | null>; addTitles: (t: Record<string, string>) => void }) {
  const v = m.value;
  if (m.source === "template") {
    return (
      <div className="nm-val">
        <input className="cfg-input" value={v || ""} onChange={(e) => set({ ...m, value: e.target.value })} aria-label={`${r.name} 模板`} />
        <div className="nm-vars">{Object.entries(view.template_vars).map(([k, d]) => (
          <button key={k} className="chip" title={d} onClick={() => set({ ...m, value: `${v || ""}{${k}}` })}>{`{${k}}`}<span className="muted"> {d}</span></button>
        ))}</div>
      </div>
    );
  }
  if (m.source === "access") {
    const val = v || {};
    return (
      <div className="nm-access">{Object.entries(view.access_labels).map(([k, label]) => (
        <label key={k} className="cfg-inline"><span className="muted">{label} →</span>
          {r.options ? (
            <select value={val[k] || ""} onChange={(e) => set({ ...m, value: { ...val, [k]: e.target.value } })}>
              <option value="">（不填）</option>
              {r.options.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          ) : <input className="cfg-input" value={val[k] || ""} onChange={(e) => set({ ...m, value: { ...val, [k]: e.target.value } })} />}
        </label>
      ))}</div>
    );
  }
  if (m.source !== "fixed") return <span className="muted">由系统按每一期计算</span>;
  if (r.type === "select" || r.type === "status") {
    return (
      <select value={v || ""} onChange={(e) => set({ ...m, value: e.target.value })} aria-label={`${r.name} 固定值`}>
        <option value="">（请选择）</option>
        {(r.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
        {v && !(r.options || []).includes(v) && <option value={v}>{v}（Notion 里已没有）</option>}
      </select>
    );
  }
  if (r.type === "multi_select") {
    const cur: string[] = v || [];
    return (
      <div className="nm-chips">{(r.options || []).map((o) => (
        <button key={o} className={`chip${cur.includes(o) ? " ink" : ""}`} aria-pressed={cur.includes(o)}
          onClick={() => set({ ...m, value: cur.includes(o) ? cur.filter((x) => x !== o) : [...cur, o] })}>{o}</button>
      ))}</div>
    );
  }
  if (r.type === "relation") return <PagePicker db={r.relation_db} value={v || []} titles={titles} onChange={(nv, t) => { addTitles(t); set({ ...m, value: nv }); }} />;
  if (r.type === "checkbox") return <label className="chk"><input type="checkbox" checked={!!v} onChange={(e) => set({ ...m, value: e.target.checked })} />勾选</label>;
  return (
    <input className="cfg-input" type={r.type === "number" ? "number" : r.type === "date" ? "date" : "text"} value={v ?? ""}
      onChange={(e) => set({ ...m, value: r.type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value })} aria-label={`${r.name} 固定值`} />
  );
}

/** Notion 交付: where this channel's issues are written and how each property of the target database is filled. */
export default function NotionMap({ cid }: { cid: string }) {
  const [view, setView] = useState<View | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, any>>({});
  const [cfg, setCfg] = useState<Cfg>({ template: "default", heading: "", icon: "none" });
  const [titles, setTitles] = useState<Record<string, string | null>>({});
  const [msg, setMsg] = useState<{ tone: "ok" | "bad" | "warn"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [onlySet, setOnlySet] = useState(false);
  const setDirtyFlag = useDirty();

  const saved = (d: View) => Object.fromEntries(d.rows.filter((r) => r.mapping).map((r) => [r.id, r.mapping]));
  const adopt = (d: View) => {
    setView(d);
    if (!d.configured) return;
    setCfg(d.config); setTitles(d.page_titles || {}); setDraft(saved(d));
  };
  const load = async () => {
    try { setErr(null); adopt(await get<View>(`/api/channels/${cid}/notion`)); } catch (e) { setErr((e as Error).message); }
  };
  useEffect(() => { load(); }, [cid]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = !!view?.configured && (JSON.stringify(draft) !== JSON.stringify(saved(view)) || JSON.stringify(cfg) !== JSON.stringify(view.config));
  useEffect(() => { setDirtyFlag("notion", dirty); return () => setDirtyFlag("notion", false); }, [dirty, setDirtyFlag]);

  if (err) return <Note tone="bad">{err} <button className="btn xs ghost" onClick={load}>重试</button></Note>;
  if (!view) return <Note>正在读取 Notion 数据库的当前属性……</Note>;
  if (!view.configured) {
    return <Note>这个频道还没有指定 Notion 数据库，交付会写入 Assetizer 自己的数据库。在服务器上用 <code>assetizer notion-target &lt;频道&gt; &lt;数据库&gt; --parent &lt;上级页面&gt;</code> 指定。</Note>;
  }
  const setRow = (r: Row, m: any | null) => setDraft((d) => { const n = { ...d }; if (m) n[r.id] = m; else delete n[r.id]; return n; });
  const pickSource = (r: Row, source: string) => {
    if (!source) return setRow(r, null);
    const init: any = { name: r.name, type: r.type, source };
    if (source === "access") init.value = Object.fromEntries(Object.entries(view.access_labels).map(([k, l]) => [k, (r.options || []).includes(l) ? l : ""]));
    if (source === "template") init.value = r.type === "title" ? "{video_title}" : "";
    if (source === "fixed") init.value = r.type === "relation" || r.type === "multi_select" ? [] : r.type === "checkbox" ? false : "";
    setRow(r, init);
  };
  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      const d = await post<View>(`/api/channels/${cid}/notion`, { properties: draft, ...cfg });
      adopt(d);
      setMsg(d.problems ? { tone: "warn", text: `已保存，但还有 ${d.problems} 处和 Notion 对不上，这个频道的交付会暂停到改好为止。` }
        : { tone: "ok", text: "已保存。下一期按新映射写入；之前因映射暂停的期会自动重试。" });
    } catch (e) { setMsg({ tone: "bad", text: (e as Error).message }); }
    setBusy(false);
  };
  const rows = onlySet ? view.rows.filter((r) => draft[r.id] || r.problem || r.deleted) : view.rows;
  const unset = view.rows.filter((r) => r.writable && !r.deleted && !draft[r.id]).length;

  return (
    <div className="nm">
      <section className="cfg-sec first">
        <div className="cfg-form">
          <Field label="目标数据库">
            <span><a href={view.database.url} target="_blank" rel="noreferrer">{view.database.title || "Notion 数据库"} ↗</a>
              <span className="muted mono"> {view.database.id.slice(0, 8)}…</span></span>
          </Field>
          <Field label="正文格式">
            <select value={cfg.template} onChange={(e) => setCfg({ ...cfg, template: e.target.value })}>
              {Object.entries(view.templates).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          </Field>
          {cfg.template !== "default" && (
            <Field label="页面大标题" hint={<>可用变量：{Object.keys(view.template_vars).map((k) => `{${k}}`).join(" ")}；留空则不加。</>}>
              <input className="cfg-input" value={cfg.heading} onChange={(e) => setCfg({ ...cfg, heading: e.target.value })} aria-label="页面大标题" />
            </Field>
          )}
          <Field label="页面图标">
            <span className="cfg-inline">
              <select value={["none", "parent"].includes(cfg.icon) ? cfg.icon : "emoji"} onChange={(e) => setCfg({ ...cfg, icon: e.target.value === "emoji" ? "📘" : e.target.value })} aria-label="页面图标">
                <option value="none">不设</option>
                <option value="parent">与上级页面相同</option>
                <option value="emoji">Emoji</option>
              </select>
              {!["none", "parent"].includes(cfg.icon) && <input className="cfg-input w-s" value={cfg.icon} onChange={(e) => setCfg({ ...cfg, icon: e.target.value })} aria-label="Emoji" />}
            </span>
          </Field>
        </div>
      </section>

      <section className="cfg-sec">
        <div className="cfg-sec-row">
          <h3 className="cfg-sec-h">属性映射</h3>
          <span className="muted">实时读取 Notion · 改属性名自动跟随{view.sample_source ? ` · 示例取自「${view.sample_source.title}」` : " · 暂无样片，示例为空"}</span>
          <label className="chk grow-r"><input type="checkbox" checked={onlySet} onChange={(e) => setOnlySet(e.target.checked)} />只看已配置{unset ? `（${unset} 个未配置）` : ""}</label>
        </div>
        {view.problems > 0 && <Note tone="bad">有 {view.problems} 处映射和 Notion 现状对不上，这个频道的交付已暂停。</Note>}
        <div className="nm-tbl" role="table" aria-label="属性映射">
          <div className="nm-tr nm-th" role="row"><span>属性</span><span>值来源</span><span>值</span><span>示例</span></div>
          {rows.map((r) => {
            const m = draft[r.id];
            return (
              <div key={r.id} className={`nm-tr${r.problem ? " bad" : ""}${!m && r.writable && !r.deleted ? " unset" : ""}`} role="row">
                <span className="nm-prop">
                  <b>{r.name}</b> <Chip>{TYPE[r.type] || r.type}</Chip>
                  {r.deleted && <Chip tone="bad">已在 Notion 删除</Chip>}
                </span>
                <span>
                  {r.deleted ? <button className="btn xs" onClick={() => setRow(r, null)}>移除映射</button>
                    : !r.writable ? <span className="muted">Notion 自动生成</span>
                    : (
                      <select value={m?.source || ""} onChange={(e) => pickSource(r, e.target.value)} aria-label={`${r.name} 的值来源`}>
                        <option value="">不填</option>
                        {r.sources.map((s) => <option key={s} value={s}>{view.source_labels[s]}</option>)}
                      </select>
                    )}
                </span>
                <span>{m && !r.deleted && <ValueEditor r={r} m={m} set={(nm) => setRow(r, nm)} view={view} titles={titles} addTitles={(t) => setTitles((x) => ({ ...x, ...t }))} />}</span>
                <span className="nm-sample">
                  {r.problem ? <span className="zhu">{r.problem}</span>
                    : m && r.mapping === m && r.sample !== null && r.sample !== undefined ? <span className="muted">{sampleText(r.sample, titles).slice(0, 240)}</span>
                    : m && r.mapping !== m ? <span className="muted">保存后显示</span> : null}
                </span>
              </div>
            );
          })}
        </div>
      </section>
      {msg && <Note tone={msg.tone}>{msg.text}</Note>}
      {dirty && (
        <div className="cfg-savebar" role="region" aria-label="保存">
          <span>有未保存的修改</span>
          <button className="btn sm ghost" onClick={() => adopt(view)}>放弃</button>
          <button className="btn sm solid" disabled={busy} onClick={save}>{busy ? "保存中…" : "保存映射"}</button>
        </div>
      )}
    </div>
  );
}
