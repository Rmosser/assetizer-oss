import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { get, post } from "../api";
import NotionMap from "../components/cfg/NotionMap";
import PlaybookPanel from "../components/cfg/Playbook";
import PromptPanel from "../components/cfg/Prompt";
import SettingsPanel from "../components/cfg/Settings";
import Workbench from "../components/cfg/Workbench";
import { Chip, DirtyCtx, Note, STAGE } from "../components/cfg/ui";
import { Fault, Stale, Unroll } from "../components/States";
import { usePoll } from "../hooks";
import { navigate, usePath } from "../router";
import { ago } from "../util";

type Tab = "workbench" | "playbook" | "prompt" | "notion" | "settings";
const TABS: { key: Tab; label: string; tracked?: boolean }[] = [
  { key: "workbench", label: "调试台", tracked: true },
  { key: "playbook", label: "规程" },
  { key: "prompt", label: "学习目标", tracked: true },
  { key: "notion", label: "Notion 交付", tracked: true },
  { key: "settings", label: "设置" },
];

const activeOf = (c: any) => c.playbooks.find((p: any) => p.status === "ACTIVE");
const draftOf = (c: any) => c.playbooks.find((p: any) => p.status === "NEEDS_CONFIRMATION");

function Side({ data, sel, go }: { data: any; sel: string; go: (to: string) => void }) {
  const tracked = data.channels.filter((c: any) => c.mode === "TRACKED");
  const others = data.channels.filter((c: any) => c.mode !== "TRACKED");
  const item = (c: any) => {
    const a = activeOf(c), d = draftOf(c);
    const st = c.mode === "TRACKED" ? STAGE[c.stage] : { label: "发现中", tone: "muted" as const };
    return (
      <a key={c.id} href={`/channels/${c.id}`} className={`cfg-ch${c.id === sel ? " on" : ""}`} aria-current={c.id === sel ? "page" : undefined}
        onClick={(e) => { if (e.metaKey || e.ctrlKey) return; e.preventDefault(); go(`/channels/${c.id}`); }}>
        <span className="cfg-ch-name">{c.name}{c.pending_playbook && <i className="cfg-dot" title="有待定稿的规程" />}</span>
        <span className="cfg-ch-meta">
          <Chip tone={st.tone}>{st.label}</Chip>
          <span>{a ? `v${a.version}` : d ? `草稿 v${d.version}` : "无规程"}</span>
          <span>{c.sources} 期</span>
          {c.samples_busy > 0 && <span className="cfg-busy">试跑 {c.samples_busy}</span>}
        </span>
      </a>
    );
  };
  return (
    <aside className="cfg-side" aria-label="频道列表">
      {tracked.length > 0 && <div className="cfg-group"><div className="cfg-group-h">长期关注</div>{tracked.map(item)}</div>}
      {others.length > 0 && <div className="cfg-group"><div className="cfg-group-h">发现中</div>{others.map(item)}</div>}
      <div className="cfg-group">
        <div className="cfg-group-h">系统</div>
        <a href="/channels/universal" className={`cfg-ch${sel === "universal" ? " on" : ""}`} aria-current={sel === "universal" ? "page" : undefined}
          onClick={(e) => { if (e.metaKey || e.ctrlKey) return; e.preventDefault(); go("/channels/universal"); }}>
          <span className="cfg-ch-name">通用规程</span>
          <span className="cfg-ch-meta"><Chip tone="ok">生效中</Chip><span>v{data.universal?.version ?? 1}</span><span>未建档频道使用</span></span>
        </a>
      </div>
    </aside>
  );
}

function Head({ c, reload }: { c: any; reload: () => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  const a = activeOf(c), d = draftOf(c);
  const tracked = c.mode === "TRACKED";
  const st = tracked ? STAGE[c.stage] : { label: "发现中", tone: "muted" as const };
  const lock = async () => {
    if (!d) return;
    let stale: string[] = [];
    try {
      const wb = await get<any>(`/api/channels/${c.id}/workbench`);
      stale = wb.samples.filter((s: any) => s.stale || !s.current_run_id).map((s: any) => `· ${s.title}`);
    } catch { /* the check is advisory */ }
    const text = [`定稿规程 v${d.version}：频道进入已定稿状态，并开启自动收录。`,
      "每期样片「采用中」的版本会写入 Notion；之后的新视频按这个版本自动生产并写入。",
      stale.length ? `\n这些样片还没有用 v${d.version} 跑过（或没有可采用的版本）：\n${stale.join("\n")}` : "",
      "\n确定定稿？"].join("\n");
    if (!confirm(text)) return;
    try { await post(`/api/playbooks/${d.id}/confirm`); setMsg(null); reload(); } catch (e) { setMsg((e as Error).message); }
  };
  const retune = async () => {
    if (!confirm("回到调试期：自动收录暂停，新的一期在重新定稿前不会写入 Notion。可以在调试台用样片试跑新版本。确定？")) return;
    try { await post(`/api/channels/${c.id}/retune`); setMsg(null); reload(); } catch (e) { setMsg((e as Error).message); }
  };
  const ing = c.ingest || {};
  return (
    <header className="cfg-head">
      <div className="cfg-head-row">
        <h1>{c.name}</h1>
        <Chip tone={st.tone}>{st.label}</Chip>
        <div className="cfg-head-acts">
          {tracked && c.stage === "TUNING" && d && <button className="btn solid sm" onClick={lock}>定稿 v{d.version} 并开启自动收录</button>}
          {tracked && c.stage === "LOCKED" && <button className="btn sm" onClick={retune}>回到调试期</button>}
        </div>
      </div>
      <dl className="cfg-facts">
        <div><dt>规程</dt><dd>{a ? <>v{a.version} <span className="muted">生效中</span></> : <span className="muted">无生效版本</span>}
          {d && <> · <span className="warn-t">草稿 v{d.version}</span></>}</dd></div>
        {tracked && <div><dt>自动收录</dt><dd>{ing.enabled ? <>开 <span className="muted">· ≥{ing.min_minutes || 10} 分钟</span></> : <span className="muted">关</span>}</dd></div>}
        {tracked && <div><dt>Notion</dt><dd>{c.notion?.configured ? (c.notion.database_title || "指定数据库") : <span className="muted">Assetizer 数据库</span>}</dd></div>}
        {tracked && <div><dt>样片</dt><dd>{c.samples}{c.samples_busy ? <span className="muted"> · {c.samples_busy} 期试跑中</span> : null}</dd></div>}
        <div><dt>已收</dt><dd>{c.sources} 期{c.last_source && <span className="muted"> · 最近 {ago(c.last_source)}</span>}</dd></div>
      </dl>
      {c.compile?.state === "running" && <Note tone="warn">学习目标 v{c.prompts[0]?.version} 正在编译成新规程（约 2–4 分钟），完成后出现在「规程」里。</Note>}
      {c.compile?.state === "failed" && <Note tone="bad">学习目标 v{c.prompts[0]?.version} 编译失败：{c.compile.error}</Note>}
      {msg && <Note tone="bad">{msg}</Note>}
    </header>
  );
}

export default function Channels() {
  const path = usePath();
  const { data, error, loadedAt, reload } = usePoll<any>("/api/channels", 0);
  const parts = path.split("/").filter(Boolean);
  const cidParam = parts[1] ? decodeURIComponent(parts[1]) : null;
  const tabParam = parts[2] as Tab | undefined;

  // unsaved edits in any panel: ask before they are thrown away
  const dirty = useRef<Set<string>>(new Set());
  const setDirty = useCallback((k: string, d: boolean) => { if (d) dirty.current.add(k); else dirty.current.delete(k); }, []);
  const go = useCallback((to: string, replace = false) => {
    if (dirty.current.size && !confirm("有未保存的修改，离开后会丢失。确定离开？")) return;
    dirty.current.clear();
    navigate(to, { replace, quiet: true });
    window.scrollTo({ top: 0 });
  }, []);
  useEffect(() => {
    const on = (e: BeforeUnloadEvent) => { if (dirty.current.size) { e.preventDefault(); e.returnValue = ""; } };
    addEventListener("beforeunload", on);
    return () => removeEventListener("beforeunload", on);
  }, []);
  // a compile in the background: check back until it lands
  const compiling = data?.channels?.some((c: any) => c.compile?.state === "running" || c.samples_busy > 0);
  useEffect(() => {
    if (!compiling) return;
    const t = setInterval(() => { if (!document.hidden) reload(); }, 15000);
    return () => clearInterval(t);
  }, [compiling, reload]);

  const fallback = useMemo(() => {
    if (!data) return null;
    const list = data.channels;
    return (list.find((c: any) => c.pending_playbook) || list.find((c: any) => c.mode === "TRACKED") || list[0])?.id || "universal";
  }, [data]);
  const sel = cidParam || fallback;
  const c = data?.channels.find((x: any) => x.id === sel);
  const tabs = TABS.filter((t) => !t.tracked || c?.mode === "TRACKED");
  const defTab: Tab = c?.mode === "TRACKED" && c.stage === "TUNING" ? "workbench" : "playbook";
  const tab: Tab = tabParam && tabs.some((t) => t.key === tabParam) ? tabParam : defTab;
  const [visited, setVisited] = useState<Set<Tab>>(new Set());
  useEffect(() => { setVisited(new Set([tab])); }, [sel]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setVisited((v) => (v.has(tab) ? v : new Set(v).add(tab))); }, [tab]);
  useEffect(() => { if (data && !cidParam && sel) navigate(`/channels/${sel}`, { replace: true, quiet: true }); }, [data, cidParam, sel]);

  if (!data) return <main className="page cfg-page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;

  return (
    <main className="page cfg-page">
      <Stale error={error} loadedAt={loadedAt} />
      <div className="cfg">
        <Side data={data} sel={sel} go={(to) => go(to)} />
        <section className="cfg-main" aria-label="频道配置">
          {sel === "universal" ? (
            <>
              <header className="cfg-head">
                <div className="cfg-head-row"><h1>通用规程</h1><Chip tone="ok">生效中</Chip></div>
                <p className="cfg-sub">没有专属学习目标的频道用这套规程：中性、完整、可追溯地整理内容，不加入任何频道偏好。只读。</p>
              </header>
              {data.universal ? <PlaybookPanel channel={null} playbooks={[data.universal]} reload={reload} />
                : <Note>没有生效的通用规程。</Note>}
            </>
          ) : !c ? (
            <Note tone="bad">找不到这个频道。</Note>
          ) : (
            <DirtyCtx.Provider value={setDirty}>
              <Head c={c} reload={reload} />
              <nav className="cfg-tabs" role="tablist" aria-label="配置项">
                {tabs.map((t) => (
                  <button key={t.key} role="tab" aria-selected={t.key === tab} className={t.key === tab ? "on" : ""}
                    onClick={() => navigate(`/channels/${c.id}/${t.key}`, { replace: true, quiet: true })}>
                    {t.label}
                    {t.key === "playbook" && c.pending_playbook && <i className="cfg-dot" aria-label="有待定稿的规程" />}
                    {t.key === "workbench" && c.samples > 0 && <span className="cfg-count">{c.samples}</span>}
                  </button>
                ))}
              </nav>
              <div key={c.id}>
                {[...visited].filter((k) => tabs.some((t) => t.key === k)).map((k) => (
                  <div key={k} role="tabpanel" hidden={k !== tab} className="cfg-panel">
                    {k === "workbench" && <Workbench cid={c.id} stage={c.stage} onChanged={reload} />}
                    {k === "playbook" && <PlaybookPanel channel={c} playbooks={c.playbooks} reload={reload} />}
                    {k === "prompt" && <PromptPanel c={c} reload={reload} />}
                    {k === "notion" && <NotionMap cid={c.id} />}
                    {k === "settings" && <SettingsPanel c={c} reload={reload} />}
                  </div>
                ))}
              </div>
            </DirtyCtx.Provider>
          )}
        </section>
      </div>
    </main>
  );
}
