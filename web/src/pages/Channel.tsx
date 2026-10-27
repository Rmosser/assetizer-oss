import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { post } from "../api";
import { Fault, Stale, Unroll } from "../components/States";
import { useLeaving, usePoll } from "../hooks";
import { navigate, usePath } from "../router";
import { reduceMotion, smooth } from "../util";
import { cn, Seg, Toast, useCompact, useNarrow } from "../components/channel/bits";
import { FIELD_OF_SOURCE, FIELD_OF_VAR, GoalCard, IntakeCard, PropsCard, StructureCard, TitleCard } from "../components/channel/Cards";
import { activeOf, draftOf, Header, modeOf, Switcher } from "../components/channel/Header";
import { keys, useLitDelegation } from "../components/channel/lit";
import { LockSheet, TargetPicker } from "../components/channel/LockSheet";
import { Manuscript } from "../components/channel/Manuscript";
import { done, Matrix, runFor, RunPending, type MatrixActions, type Want } from "../components/channel/Matrix";
import { NotionPane, type PaneHead } from "../components/channel/NotionPane";
import { SourcePane } from "../components/channel/SourcePane";
import { DiscoveryBar, LockedBar } from "../components/channel/StateBars";
import { Wires } from "../components/channel/Wires";
import type {
  Annotations, Channel, ChannelsResp, Compare, Draft, Issue, Mapping, NotionView, Preview, PropRow, Run, SourceDetail, Workbench,
} from "../components/channel/types";
import "../channel.css";

type View = "studio" | "manuscript" | "rules";

/** What one run becomes in Notion under the saved config plus unsaved card edits (debounced while typing). */
function usePreview(cid: string, runId: string | null, draft: Draft | null) {
  const [st, setSt] = useState<{ data: Preview | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: false });
  const body = JSON.stringify({ run_id: runId, config: draft });
  useEffect(() => {
    if (!runId) { setSt({ data: null, error: null, loading: false }); return; }
    let alive = true;
    setSt((s) => ({ ...s, loading: true }));
    const t = window.setTimeout(async () => {
      try {
        const d = await post<Preview>(`/api/channels/${cid}/notion/preview`, JSON.parse(body));
        if (alive) setSt({ data: d, error: null, loading: false });
      } catch (e) { if (alive) setSt((s) => ({ data: s.data, error: (e as Error).message, loading: false })); }
    }, draft ? 300 : 0);
    return () => { alive = false; clearTimeout(t); };
  }, [cid, body]); // eslint-disable-line react-hooks/exhaustive-deps
  return st;
}

const label = (r: { label?: string; rule: string }) => r.label || (r.rule.length > 10 ? r.rule.slice(0, 8) + "…" : r.rule);

let morphs = 0;
/** The 稿纸 opening or closing as one view transition: the 学习目标 card grows into the sheet and folds back into it
 *  (channel.css, 「稿纸开合」). `cls` stay on <html> while it plays and pick what takes part; `after` runs once the page
 *  shows the new state, before the browser captures it. Without the API, or under reduced motion, it just happens. */
function morph(update: () => void, cls: string[], after?: () => void) {
  if (!("startViewTransition" in document) || reduceMotion()) { update(); after?.(); return; }
  const root = document.documentElement, n = ++morphs;
  root.classList.remove("ch-vt-open", "ch-vt-close", "ch-vt-col");
  root.classList.add("ch-vt", ...cls);
  document.startViewTransition(() => { flushSync(update); after?.(); }).finished
    .finally(() => { if (n === morphs) root.classList.remove("ch-vt", ...cls); }).catch(() => {});
}

/** 通用规程: what a channel without its own playbook is made with. */
function Universal({ list }: { list: ChannelsResp }) {
  const pb = list.universal;
  return (
    <div className="chp">
      <header className="ch-head">
        <Switcher list={list} current="universal"><h1>通用规程</h1></Switcher>
        <span className="ch-chip ink">生效中{pb ? ` · v${pb.version}` : ""}</span>
      </header>
      <div className="ch-msh solo"><div className="msb"><div className="rv">
        {!pb && <p className="q">没有生效的通用规程。</p>}
        {pb && pb.rules.length > 0 && <>
          <div className="rvh">规则</div>
          {pb.rules.map((r, i) => <div key={r.id || i} className="rvi"><div><b>{label(r)}</b></div><div className="tx">{r.rule}</div></div>)}
        </>}
        {pb?.article_style && <><div className="rvh">文章写法</div><div className="rvi"><div><b>所有频道</b></div><div className="tx">{pb.article_style}</div></div></>}
        {pb && pb.facets.length > 0 && <>
          <div className="rvh">频道字段</div>
          {pb.facets.map((f) => <div key={f.key} className="rvi"><div><b>{f.label}</b></div><div className="tx">{f.description}</div></div>)}
        </>}
      </div></div></div>
    </div>
  );
}

/** 频道: one channel as a visible pipeline — the YouTube source, five setting cards, the Notion page they produce. */
export default function ChannelPage() {
  const path = usePath();
  const parts = path.split("/").filter(Boolean);
  const cidParam = parts[1] ? decodeURIComponent(parts[1]) : null;
  const { data: list, error, loadedAt, reload } = usePoll<ChannelsResp>("/api/channels", 0);
  useEffect(() => {  // links to the old tabs land on the same content
    if (!parts[2]) return;
    const q = ({ prompt: "?view=manuscript", playbook: "?view=rules", settings: "?open=intake" } as Record<string, string>)[parts[2]] || "";
    navigate(`/channels/${parts[1]}${q}`, { replace: true, quiet: true });
  }, [path]); // eslint-disable-line react-hooks/exhaustive-deps
  const busy = list?.channels.some((c) => c.compile?.state === "running" || c.samples_busy > 0);
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(() => { if (!document.hidden) reload(); }, 15000);
    return () => clearInterval(t);
  }, [busy, reload]);
  const fallback = useMemo(() => {
    if (!list) return null;
    const l = list.channels;
    return (l.find((c) => c.pending_playbook) || l.find((c) => c.mode === "TRACKED") || l[0])?.id || "universal";
  }, [list]);
  const sel = cidParam || fallback;
  useEffect(() => { if (list && !cidParam && sel) navigate(`/channels/${encodeURIComponent(sel)}`, { replace: true, quiet: true }); }, [list, cidParam, sel]);
  if (!list) return <main className="page chp-page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const ch = list.channels.find((c) => c.id === sel);
  return (
    <main className="page chp-page">
      <Stale error={error} loadedAt={loadedAt} />
      {sel === "universal" ? <Universal list={list} />
        : !ch ? <p className="ch-err pad">找不到这个频道。</p>
        : <ChannelView key={ch.id} ch={ch} list={list} reloadList={reload} />}
    </main>
  );
}

function ChannelView({ ch, list, reloadList }: { ch: Channel; list: ChannelsResp; reloadList: () => void }) {
  const mode = modeOf(ch);
  const qs = new URLSearchParams(location.search);
  const [view, setView] = useState<View>(["manuscript", "rules"].includes(qs.get("view") || "") ? (qs.get("view") as View) : "studio");
  const [sel, setSel] = useState<{ sample: string | null; run: string | null }>({ sample: qs.get("sample"), run: qs.get("run") });
  const [cmp, setCmp] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [sheet, setSheet] = useState(false);
  const [target, setTarget] = useState(false);
  const [toast, setToast] = useState<{ text: string; seal?: boolean } | null>(null);
  const [msPb, setMsPb] = useState<number | null>(Number(qs.get("pb")) || null);
  const [focus, setFocus] = useState<string | null>(null);
  const [initialText, setInitialText] = useState<string | null>(null);
  const [titles, setTitles] = useState<Record<string, string | null>>({});
  const [tab, setTab] = useState<"notion" | "source">("notion");
  const [want, setWant] = useState<Want | null>(null);
  const narrow = useNarrow();
  const compact = useCompact();
  const studio = useRef<HTMLDivElement>(null);
  const pane = useRef<HTMLDivElement>(null);
  const toastT = useRef<number | undefined>(undefined);
  useLitDelegation(studio);
  const flash = useCallback((text: string, seal?: boolean) => {
    setToast({ text, seal });
    clearTimeout(toastT.current);
    toastT.current = window.setTimeout(() => setToast(null), 4200);
  }, []);

  // ------------------------------------------------------------------ data
  const wbQ = usePoll<Workbench>(mode === "tuning" ? `/api/channels/${ch.id}/workbench` : null, 0);
  const wb = wbQ.data;
  const wbBusy = !!wb && (wb.samples.some((s) => s.busy) || wb.queue.length > 0);
  useEffect(() => {
    if (!wbBusy) return;
    const t = setInterval(() => { if (!document.hidden) wbQ.reload(); }, 15000);
    return () => clearInterval(t);
  }, [wbBusy, wbQ.reload]); // eslint-disable-line react-hooks/exhaustive-deps
  const recentQ = usePoll<{ issues: Issue[] }>(mode !== "tuning" ? `/api/channels/${ch.id}/recent?limit=4` : null, 0);
  const issues = recentQ.data?.issues || [];
  const notionQ = usePoll<NotionView>(`/api/channels/${ch.id}/notion`, 0);
  const nv = notionQ.data?.configured ? notionQ.data : null;
  useEffect(() => { if (nv?.page_titles) setTitles((t) => ({ ...nv.page_titles, ...t })); }, [nv]);
  const overview = usePoll<any>("/api/overview", 30000);
  const alerts: string[] = (overview.data?.alerts || []).map((a: any) => a.key).filter((k: string) => ["notion-auth", "YOUTUBE_AUTH", "AGY_AUTH_REQUIRED", "DISK_LOW"].includes(k));

  const draftPb = draftOf(ch) || null, activePb = activeOf(ch) || null;
  const goalPb = mode === "locked" ? activePb : draftPb || activePb;
  const annQ = usePoll<Annotations>(goalPb ? `/api/playbooks/${goalPb.id}/annotations` : null, 0);
  const newerDraft = mode === "locked" && draftPb && activePb && draftPb.version > activePb.version ? draftPb : null;

  // ------------------------------------------------------------------ the selected cell / issue
  const samples = wb?.samples || [];
  let sample = samples.find((s) => s.id === sel.sample) || null;
  let run: Run | null = sample ? sample.runs.find((r) => r.id === sel.run) || null : null;
  if (mode === "tuning" && !done(run)) {
    sample = (sample && sample.runs.some(done) ? sample : samples.find((s) => s.runs.some(done))) || samples[0] || null;
    run = sample ? sample.runs.find((r) => r.id === sample!.adopt.run_id && done(r)) || sample.runs.find(done) || null : null;
  }
  const issue = mode !== "tuning" ? issues.find((i) => i.id === sel.sample) || issues[0] || null : null;
  const sampleId = mode === "tuning" ? sample?.id ?? null : issue?.id ?? null;
  const runId = mode === "tuning" ? run?.id ?? null : issue?.current_run_id ?? null;
  const detailQ = usePoll<SourceDetail>(sampleId && runId ? `/api/sources/${encodeURIComponent(sampleId)}?run=${encodeURIComponent(runId)}` : null, 0);
  // a newly picked cell keeps the one before on screen, dimmed, until its own detail arrives
  const detail = sampleId && runId ? detailQ.data : null;
  const detailLoading = !!detail && detail.run?.id !== runId;

  useEffect(() => {  // where the page is, in its address (survives a reload, can be shared)
    const p = new URLSearchParams();
    if (view !== "studio") p.set("view", view);
    if (view !== "studio" && msPb) p.set("pb", String(msPb));
    if (sampleId && sel.sample) p.set("sample", sampleId);
    if (runId && sel.run) p.set("run", runId);
    const q = p.toString();
    history.replaceState(history.state, "", `${location.pathname}${q ? `?${q}` : ""}`);
  }, [view, msPb, sampleId, runId, sel.sample, sel.run]);

  useEffect(() => {  // the run a phone was waiting for finished: show it
    const s = want && samples.find((x) => x.id === want.sample);
    const r = s ? runFor(s, want!.pb) : null;
    if (s && done(r)) { setSel({ sample: s.id, run: r.id }); setWant(null); }
  }, [wb, want]); // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------------ Notion settings: saved + unsaved card edits
  const saved: Draft | null = useMemo(() => (nv ? {
    properties: Object.fromEntries(nv.rows.filter((r) => r.mapping).map((r) => [r.id, r.mapping as Mapping])),
    template: nv.config.template, heading: nv.config.heading || "", icon: nv.config.icon || "none",
  } : null), [nv]);
  const cfg = draft ?? saved;
  const dirty = !!draft && !!saved && JSON.stringify(draft) !== JSON.stringify(saved);
  useEffect(() => {
    if (!dirty) return;
    const on = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    addEventListener("beforeunload", on);
    return () => removeEventListener("beforeunload", on);
  }, [dirty]);
  const update = (p: Partial<Draft>) => setDraft((d) => ({ ...(d ?? saved!), ...p }));
  const setProp = (pid: string, m: Mapping | null) => setDraft((d) => {
    const base = d ?? saved!;
    const props = { ...base.properties };
    if (m) props[pid] = m; else delete props[pid];
    return { ...base, properties: props };
  });
  const pv = usePreview(ch.id, runId, dirty ? draft : null);
  const preview = pv.data?.configured ? pv.data : null;
  useEffect(() => {  // a paused delivery opens the property that stops it
    if (!preview || open) return;
    const bad = Object.keys(preview.problems)[0];
    if (bad && ch.blocked > 0) setOpen(bad);
  }, [preview, ch.blocked]); // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------------ comparison with the version before
  const prevRun = (() => {
    if (mode !== "tuning" || !sample || !run?.version) return null;
    const older = (wb?.playbooks || []).filter((p) => p.version < run!.version!).sort((a, b) => b.version - a.version);
    for (const p of older) { const r = runFor(sample, p.id); if (done(r)) return r; }
    return null;
  })();
  const cmpQ = usePoll<Compare & { a: { id: string }; b: { id: string } }>(cmp && prevRun && runId ? `/api/compare?a=${prevRun.id}&b=${runId}` : null, 0);
  const cmpData = cmp && prevRun && cmpQ.data?.a?.id === prevRun.id && cmpQ.data?.b?.id === runId ? cmpQ.data : null;

  // ------------------------------------------------------------------ chapters: which section of the page each unit went into
  const { secOf, merged } = useMemo(() => {
    const m = new Map<string, number>();
    let i = -1, last = "", many = false;
    for (const b of preview?.blocks || []) {
      if (b.origin !== "unit") continue;
      const head = b.key.split(":")[0];
      if (head !== last) { i++; last = head; if (b.unit_ids.length > 1) many = true; }
      b.unit_ids.forEach((u) => { if (!m.has(u)) m.set(u, i); });
    }
    return { secOf: m, merged: many };
  }, [preview]);
  const badge = (i: number) => (!merged ? null : cfg?.template === "chapters" ? `第${cn(i + 1)}章` : cfg?.template === "reading-guide" ? String(i + 1).padStart(2, "0") : `#${i + 1}`);
  const jump = (i: number) => {
    const el = pane.current?.querySelector(`[data-k~="c:${i}"]`) as HTMLElement | null;
    if (pane.current && el) pane.current.scrollTo({ top: el.offsetTop - 16, behavior: smooth() });
  };

  // ------------------------------------------------------------------ keys tying a fact across the three panes
  const propsOf = (f: string) => Object.entries(cfg?.properties || {}).filter(([, m]) => FIELD_OF_SOURCE[m.source] === f).map(([pid]) => `p:${pid}`);
  const varsOf = (f: string) => Object.entries(FIELD_OF_VAR).filter(([, x]) => x === f).map(([v]) => `tok:${v}`);
  const fk = (f: string) => keys(f, ...varsOf(f), ...propsOf(f));
  const fieldKeys = { url: fk("f:url"), title: fk("f:title"), channel: fk("f:channel"), date: fk("f:date"), access: fk("f:access") };
  const propKey = (r: PropRow) => keys(`p:${r.id}`, r.mapping && FIELD_OF_SOURCE[r.mapping.source]);
  const titleRow = nv?.rows.find((r) => r.type === "title");
  const titleKey = titleRow ? `p:${titleRow.id}` : "";
  const fieldOf = (v: string) => FIELD_OF_VAR[v] || null;

  // ------------------------------------------------------------------ actions
  const guard = <A extends unknown[]>(fn: (...a: A) => Promise<void>) => async (...a: A) => {
    try { await fn(...a); } catch (e) { flash((e as Error).message); }
  };
  const act: MatrixActions = {
    trial: guard(async (pids: number[], sids?: string[]) => {
      wbQ.setData(await post<Workbench>(`/api/channels/${ch.id}/trial`, { playbook_ids: pids, source_ids: sids || null }));
      reloadList(); flash("已排进试跑队列");
    }),
    add: async (url: string) => { wbQ.setData(await post<Workbench>(`/api/channels/${ch.id}/samples`, { url })); reloadList(); },
    remove: guard(async (sid: string) => { wbQ.setData(await post<Workbench>(`/api/channels/${ch.id}/samples/${encodeURIComponent(sid)}/remove`)); reloadList(); }),
    clear: guard(async () => { wbQ.setData(await post<Workbench>(`/api/channels/${ch.id}/trial-queue/clear`)); }),
    retry: guard(async (rid: string) => { await post(`/api/runs/${encodeURIComponent(rid)}/retry`); wbQ.reload(); }),
  };
  const adopt = guard(async (sid: string, rid: string | null, pinned: boolean) => {
    await post(`/api/sources/${encodeURIComponent(sid)}/adopt`, { run_id: rid, pinned });
    wbQ.reload();
  });
  const saveNotion = guard(async () => {
    if (!draft) return;
    const v = await post<NotionView>(`/api/channels/${ch.id}/notion`, draft);
    notionQ.setData(v); setDraft(null); reloadList();
    flash(v.problems ? `已保存，还有 ${v.problems} 处和 Notion 对不上` : mode === "locked" ? "已保存 · 下一期起按新设置写入" : "已保存");
  });
  const retune = async () => { await post(`/api/channels/${ch.id}/retune`); reloadList(); flash("已回到调试期 · 自动收录已暂停"); };
  const setIngest = async (enabled: boolean, min: number) => { await post(`/api/channels/${ch.id}/ingest`, { enabled, min_minutes: min }); reloadList(); };
  const savePrompt = async (text: string, note: string) => {
    if (ch.mode === "DISCOVERY") {
      if (!issues[0]) throw new Error("先付印一期这个频道的视频");
      await post("/api/channels/track", { name: ch.name, why: ch.why || "", prompt: text, source_id: issues[0].id });
    } else {
      await post(`/api/channels/${ch.id}/prompt`, { text, note: note || null });
    }
    setInitialText(null); reloadList(); flash("已保存 · 正在编译新版本，约 3 分钟");
  };
  const draftGoal = async (why: string) => {
    await post(`/api/channels/${ch.id}`, { why });
    const r = await post<{ raw_prompt: string }>("/api/channels/draft", { name: ch.name, why, source_id: issues[0]?.id || null });
    sheetOpen(() => { setInitialText(r.raw_prompt); setView("manuscript"); }); reloadList();
  };
  const recompile = async (pbId: number) => {
    await post(`/api/playbooks/${pbId}/recompile`);
    reloadList(); flash("正在按这份学习目标重新编译 · 约 3 分钟后多出一版草稿");
  };
  // the sheet grows out of the 学习目标 card and folds back into it. Afterwards the toggle in their shared corner has the
  // focus; on a phone, where nothing sits side by side, what took the tapped thing's place is scrolled into view.
  const settle = (toggle: string, show: string) => {
    document.querySelector<HTMLElement>(toggle)?.focus({ preventScroll: true });
    const r = narrow ? document.querySelector(show)?.getBoundingClientRect() : null;
    if (r && (r.top < 0 || r.top > innerHeight - 80)) document.querySelector(show)?.scrollIntoView({ block: "start" });
  };
  const sheetOpen = (go: () => void) => {
    if (view !== "studio") { go(); return; }
    // a card scrolled partly out of its column would be drawn whole while it grows: the column grows instead
    const col = studio.current?.querySelector(".ch-cards"), card = col?.querySelector(".ch-goal");
    const cut = !!col && !!card && card.getBoundingClientRect().top < col.getBoundingClientRect().top - 1;
    morph(go, cut ? ["ch-vt-open", "ch-vt-col"] : ["ch-vt-open"], () => settle(".ch-msh .ch-msx", ".ch-studio"));
  };
  const openMs = (v: "manuscript" | "rules", f?: string) => sheetOpen(() => { setMsPb(goalPb?.id ?? null); setFocus(f || null); setView(v); });
  const closeMs = () => morph(() => { setView("studio"); setFocus(null); }, ["ch-vt-close"], () => settle(".ch-goal .ch-msx", ".ch-goal"));

  // ------------------------------------------------------------------ what each pane shows
  const version = mode === "tuning" ? (run?.version ? `v${run.version}` : "通用") : activePb ? `v${activePb.version}` : "通用";
  const track: [number, number, number][] = mode === "tuning" && run && !detailLoading ? run.track
    : (detail?.units || []).map((u) => [u.start, u.end, u.learning_priority || 2]);
  const unitMid = new Map((detail?.units || []).map((u) => [u.id, (u.start + u.end) / 2]));
  const flags = mode === "tuning" && run && !detailLoading ? run.flag_at
    : (detail?.flags || []).filter((f) => f.severity !== "INFO" && unitMid.has(f.target_id)).map((f) => unitMid.get(f.target_id) as number);
  const head: PaneHead | null = mode === "tuning" && sample && run ? (() => {
    const adopted = sample.adopt.run_id === run.id;
    return {
      version, adopted: adopted ? { pinned: sample.adopt.pinned } : null, canAdopt: !adopted, canPin: adopted && !sample.adopt.pinned,
      canUnpin: adopted && sample.adopt.pinned, onAdopt: () => adopt(sample.id, run.id, true), onPin: () => adopt(sample.id, run.id, true),
      onUnpin: () => adopt(sample.id, null, false), cmpVersion: prevRun?.version ?? null, cmp, onCmp: () => setCmp(!cmp),
      link: { href: `/issue/${encodeURIComponent(sample.id)}?run=${encodeURIComponent(run.id)}`, text: "对照视图", external: false },
    };
  })() : issue ? {
    version, adopted: null, canAdopt: false, canPin: false, canUnpin: false, onAdopt() {}, onPin() {}, onUnpin() {},
    cmpVersion: null, cmp: false, onCmp() {},
    link: issue.notion_url ? { href: issue.notion_url, text: "在 Notion 打开", external: true } : { href: `/issue/${encodeURIComponent(issue.id)}`, text: "对照视图", external: false },
  } : null;
  const notionEmpty = pv.data && !pv.data.configured
    ? <div className="ch-noconf"><p>这个频道还没有 Notion 目标。</p><button className="ch-btn sm solid" onClick={() => setTarget(true)}>选一个数据库</button></div>
    : !runId ? <div className="ch-noconf"><p>{mode === "tuning" ? "选一格跑完的样片看它写进 Notion 的样子。" : "还没有可以预览的一期。"}</p></div> : null;
  const testCopy = !!(notionQ.data as any)?.test_copy;
  // wide: source | cards | Notion; narrower: cards | one preview with a switch; phones: the preview above the cards
  const switcher = compact ? <Seg small label="预览" value={tab} onChange={setTab} options={[{ value: "notion", label: "Notion 页面" }, { value: "source", label: "原片" }]} /> : null;
  const notionPane = (
    <NotionPane preview={preview} error={pv.error} loading={pv.loading} head={head} cmp={cmpData} secOf={secOf} propKey={propKey}
      titleKey={titleKey} fieldOf={fieldOf} paneRef={pane} empty={notionEmpty} switcher={switcher} />
  );
  const sourcePane = (
    <SourcePane detail={detail} loading={detailLoading || (!detail && !!(sampleId && runId))} channelName={ch.name} track={track} flags={flags} version={version} cmp={cmpData}
      prevVersion={prevRun?.version ?? null} secOf={secOf} badge={badge} keys={fieldKeys} onJump={jump} switcher={switcher} />
  );
  const turn = narrow && want && wb ? <RunPending wb={wb} want={want} act={act} switcher={switcher} /> : tab === "source" ? sourcePane : notionPane;
  const lockPb = draftPb || activePb;
  const dock = useLeaving(dirty && view === "studio");
  const toastOut = useLeaving(toast);

  return (
    <div className="chp">
      <Header ch={ch} list={list} alerts={alerts} testCopy={testCopy} lockVersion={mode === "tuning" ? lockPb?.version ?? null : null}
        onLock={() => setSheet(true)} onRetune={retune} reload={reloadList} />
      {mode === "tuning" && (wb
        ? <Matrix wb={wb} ch={ch} sel={{ sample: sample?.id ?? null, run: run?.id ?? null }} cmp={cmpData ? prevRun?.id ?? null : null} act={act}
            want={narrow ? want : null} onWant={setWant} onPick={(s, r) => { setSel({ sample: s, run: r }); setCmp(false); setWant(null); }} />
        : <section className="ch-tb loading" aria-busy="true" />)}
      {mode === "locked" && <LockedBar issues={issues} selected={issue?.id ?? null} onPick={(i) => setSel({ sample: i.id, run: i.current_run_id })} />}
      {mode === "discovery" && <DiscoveryBar issue={issue} />}
      <div className={`ch-studio${view !== "studio" ? " ms" : ""}`} ref={studio}>
        {view === "studio" ? (
          <>
            {!compact ? sourcePane : narrow ? turn : null}
            <div className="ch-cards" aria-label="设置">
              <GoalCard mode={mode} ch={ch} pb={goalPb} ann={annQ.data && annQ.data.playbook.id === goalPb?.id ? annQ.data : null}
                newerDraft={newerDraft} onOpen={openMs} onRetune={() => retune().catch((e) => flash(e.message))} onDraft={draftGoal} />
              {nv && cfg ? (
                <>
                  <TitleCard view={nv} cfg={cfg} setProp={setProp} ctx={preview?.context || {}} />
                  <PropsCard view={nv} cfg={cfg} setProp={setProp} preview={preview} titles={titles} addTitles={(t) => setTitles((x) => ({ ...x, ...t }))}
                    testCopy={testCopy} open={open} setOpen={setOpen} onRetarget={() => setTarget(true)} />
                  <StructureCard view={nv} cfg={cfg} update={update} ctx={preview?.context || {}} />
                </>
              ) : notionQ.data && !notionQ.data.configured ? (
                <section className="ch-card"><div className="ch-cardh"><span className="cn">贰</span><h3>Notion</h3></div>
                  <p className="q">还没有指定写入哪个 Notion 数据库。</p><button className="ch-btn sm solid" onClick={() => setTarget(true)}>选一个数据库</button></section>
              ) : notionQ.error ? <section className="ch-card"><p className="ch-err">{notionQ.error.message}</p></section> : null}
              <IntakeCard ch={ch} mode={mode} onSet={setIngest} />
            </div>
          </>
        ) : (
          <Manuscript ch={ch} mode={mode} pbId={msPb ?? goalPb?.id ?? null} setPbId={setMsPb} view={view} setView={setView} focus={focus}
            seed={annQ.data} onClose={closeMs} onRetune={() => retune().catch((e) => flash(e.message))} onSave={savePrompt}
            onRecompile={recompile} initialText={initialText} />
        )}
        {view !== "studio" || !compact ? notionPane : !narrow ? turn : null}
        <Wires host={studio} />
      </div>
      {dock.shown && (
        <div className={`ch-dock${dock.leaving ? " leaving" : ""}`} role="region" aria-label="保存" inert={dock.leaving}>
          <span className="pulse" aria-hidden />
          <span>Notion 设置有改动{mode === "locked" ? " · 下一期起生效" : ""}</span>
          <button className="ch-btn sm" onClick={() => setDraft(null)}>放弃</button>
          <button className="ch-btn sm solid" onClick={saveNotion}>保存</button>
        </div>
      )}
      {sheet && lockPb && (
        <LockSheet cid={ch.id} defaultPb={lockPb.id} onClose={() => setSheet(false)}
          onLocked={(r) => { setSheet(false); reloadList(); flash(`已定稿 v${r.version} · 写入 ${r.write} 期${r.ingest?.enabled ? " · 自动收录已开启" : ""}`, true); }}
          onConflict={(c) => { setSheet(false); openMs("manuscript", `cl:${c}`); }} onRetarget={() => { setSheet(false); setTarget(true); }}
          onTrial={async (pid) => { await act.trial([pid]); }} />
      )}
      {target && (
        <TargetPicker cid={ch.id} current={nv?.database.id || null} onClose={() => setTarget(false)}
          onDone={(v) => { setTarget(false); notionQ.setData(v); setDraft(null); reloadList(); flash(v.repick?.length ? `已换库 · 需要重选：${v.repick.join("、")}` : "已换库"); }} />
      )}
      {toastOut.shown && <Toast text={toastOut.shown.text} seal={toastOut.shown.seal} leaving={toastOut.leaving} />}
    </div>
  );
}
