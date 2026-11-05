import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { HttpError, post } from "../api";
import { Fault, Stale, Unroll } from "../components/States";
import { useLeaving, usePoll, useBack } from "../hooks";
import { navigate, usePath } from "../router";
import { reduceMotion, smooth } from "../util";
import { cn, Seg, Toast, useCompact, useNarrow } from "../components/channel/bits";
import { FIELD_OF_SOURCE, FIELD_OF_VAR, GoalCard, IntakeCard, PropsCard, StructureCard, TitleCard } from "../components/channel/Cards";
import { activeOf, draftOf, Header, modeOf, Switcher } from "../components/channel/Header";
import { keepPlace, useLinkedScroll } from "../components/channel/linkScroll";
import { keys, useLitDelegation } from "../components/channel/lit";
import { LockSheet, TargetPicker } from "../components/channel/LockSheet";
import { Manuscript } from "../components/channel/Manuscript";
import { Current, done, Matrix, runFor, RunPending, type MatrixActions, type Want } from "../components/channel/Matrix";
import { NotionPane, type PaneHead } from "../components/channel/NotionPane";
import { SourcePane } from "../components/channel/SourcePane";
import { Board } from "../components/channel/Board";
import { Overview } from "../components/channel/Overview";
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
  root.classList.remove("ch-vt-open", "ch-vt-close", "ch-vt-col", "ch-vt-nt");
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

type Page = "board" | "studio";

/** 频道: every channel (/channels), and one channel as its 看板 (/board, what a locked channel brings in) or as a visible
 *  pipeline (/studio) — the YouTube source, five setting cards, the Notion page they produce. */
export default function ChannelPage() {
  const path = usePath();
  const parts = path.split("/").filter(Boolean);
  const cidParam = parts[1] ? decodeURIComponent(parts[1]) : null;
  const sub = parts[2] === "board" || parts[2] === "studio" ? parts[2] : null;
  const { data: list, error, loadedAt, reload } = usePoll<ChannelsResp>("/api/channels", 0);
  useEffect(() => {  // links to the old tabs land on the same content
    if (!parts[2] || sub) return;
    const to = ({ prompt: "?view=manuscript", playbook: "?view=rules", settings: "/board" } as Record<string, string>)[parts[2]] || "";
    navigate(`/channels/${parts[1]}${to}`, { replace: true, quiet: true });
  }, [path]); // eslint-disable-line react-hooks/exhaustive-deps
  const busy = list?.channels.some((c) => c.compile?.state === "running" || c.samples_busy > 0);
  useEffect(() => {  // idle too, slower: a lock or a trial from another device shows here without a reload
    const t = setInterval(() => { if (!document.hidden) reload(); }, busy ? 15000 : 60000);
    return () => clearInterval(t);
  }, [busy, reload]);
  useBack(reload);
  useEffect(() => {  // the overview's figures and lamps follow intake and production
    if (cidParam) return;
    const t = setInterval(() => { if (!document.hidden) reload(); }, 30000);
    return () => clearInterval(t);
  }, [cidParam, reload]);
  if (!list) return <main className="page chp-page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const ch = list.channels.find((c) => c.id === cidParam);
  return (
    <main className="page chp-page">
      <Stale error={error} loadedAt={loadedAt} />
      {!cidParam ? <Overview list={list} />
        : cidParam === "universal" ? <Universal list={list} />
        : !ch ? <p className="ch-err pad">找不到这个频道。</p>
        : <ChannelView key={ch.id} ch={ch} list={list} reloadList={reload} sub={sub} />}
    </main>
  );
}

/** Notion settings changed and not saved yet, per channel: they wait for you while you look at another channel or
 *  another page of the app, and come back with their save bar (a reload or a closed tab asks first, below). */
const unsaved = new Map<string, Draft>();

function ChannelView({ ch, list, reloadList, sub }: { ch: Channel; list: ChannelsResp; reloadList: () => void; sub: Page | null }) {
  const mode = modeOf(ch);
  const qs = new URLSearchParams(location.search);
  // a locked channel opens on its 看板; a link into the 调试台 (a sample, a run, the 稿纸) still lands there
  const page: Page = mode !== "locked" ? "studio" : sub ?? (["view", "sample", "run", "pb"].some((k) => qs.has(k)) ? "studio" : "board");
  const [studioSeen, setStudioSeen] = useState(page === "studio");
  useEffect(() => { if (page === "studio") setStudioSeen(true); }, [page]);
  useEffect(() => {  // the address names a locked channel's page, so tidying the 调试台's query never turns it into the 看板
    const want = mode === "locked" ? `/${page}` : "";
    if ((sub ? `/${sub}` : "") !== want) navigate(`/channels/${encodeURIComponent(ch.id)}${want}${location.search}`, { replace: true, quiet: true });
  }, [mode, sub, page, ch.id]);
  const [view, setView] = useState<View>(["manuscript", "rules"].includes(qs.get("view") || "") ? (qs.get("view") as View) : "studio");
  const [sel, setSel] = useState<{ sample: string | null; run: string | null }>({ sample: qs.get("sample"), run: qs.get("run") });
  const [cmp, setCmp] = useState(false);
  const [spread, setSpread] = useState(qs.get("spread") === "1");
  const [vs, setVs] = useState<string | null>(qs.get("vs"));
  const [linked, setLinked] = useState(true);
  const [draft, setDraft] = useState<Draft | null>(() => unsaved.get(ch.id) ?? null);
  useEffect(() => { if (draft) unsaved.set(ch.id, draft); else unsaved.delete(ch.id); }, [draft, ch.id]);
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
  const vsPane = useRef<HTMLDivElement>(null);
  const bar = useRef<HTMLElement>(null);
  const toastT = useRef<number | undefined>(undefined);
  const inflight = useRef(new Set<string>());  // requests that spend model quota, under way
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
    if (mode !== "tuning") return;
    const t = setInterval(() => { if (!document.hidden) wbQ.reload(); }, wbBusy ? 15000 : 60000);
    return () => clearInterval(t);
  }, [mode, wbBusy, wbQ.reload]); // eslint-disable-line react-hooks/exhaustive-deps
  useBack(mode === "tuning" ? wbQ.reload : null);
  // the 调试台's reads wait until it is first shown: a locked channel opens on its 看板
  const recentQ = usePoll<{ issues: Issue[] }>(mode !== "tuning" && studioSeen ? `/api/channels/${ch.id}/recent?limit=4` : null, 0);
  const issues = recentQ.data?.issues || [];
  const notionQ = usePoll<NotionView>(studioSeen ? `/api/channels/${ch.id}/notion` : null, 0);
  const nv = notionQ.data?.configured ? notionQ.data : null;
  useEffect(() => { if (nv?.page_titles) setTitles((t) => ({ ...nv.page_titles, ...t })); }, [nv]);
  const status = usePoll<any>("/api/status", 30000);
  const alerts: string[] = (status.data?.alerts || []).map((a: any) => a.key).filter((k: string) => ["notion-auth", "YOUTUBE_AUTH", "AGY_AUTH_REQUIRED", "DISK_LOW"].includes(k));

  // ------------------------------------------------------------------ the selected cell / issue
  const samples = wb?.samples || [];
  let sample = samples.find((s) => s.id === sel.sample) || null;
  let run: Run | null = sample ? sample.runs.find((r) => r.id === sel.run) || null : null;
  if (mode === "tuning" && !done(run)) {
    sample = (sample && sample.runs.some(done) ? sample : samples.find((s) => s.runs.some(done))) || samples[0] || null;
    run = sample ? sample.runs.find((r) => r.id === sample!.adopt.run_id && done(r)) || sample.runs.find(done) || null : null;
  }
  const issue = mode !== "tuning" ? issues.find((i) => i.id === sel.sample) || issues[0] || null : null;

  const draftPb = draftOf(ch) || null, activePb = activeOf(ch) || null;
  // 壹 speaks of the version on screen, the same one as the source and the Notion page beside it
  const goalPb = mode === "locked" ? activePb : (run && ch.playbooks.find((p) => p.id === run!.playbook_id)) || draftPb || activePb;
  const annQ = usePoll<Annotations>(goalPb && studioSeen ? `/api/playbooks/${goalPb.id}/annotations` : null, 0);
  // versions do not change: one seen before shows at once, and stepping through them does not empty the card in between
  const annSeen = useRef(new Map<number, Annotations>());
  if (annQ.data) annSeen.current.set(annQ.data.playbook.id, annQ.data);
  const goalAnn = (goalPb && annSeen.current.get(goalPb.id)) || null;
  const newerDraft = mode === "locked" && draftPb && activePb && draftPb.version > activePb.version ? draftPb : null;
  const sampleId = mode === "tuning" ? sample?.id ?? null : issue?.id ?? null;
  const runId = mode === "tuning" ? run?.id ?? null : issue?.current_run_id ?? null;
  const detailQ = usePoll<SourceDetail>(sampleId && runId ? `/api/sources/${encodeURIComponent(sampleId)}?run=${encodeURIComponent(runId)}` : null, 0);
  // a newly picked cell keeps the one before on screen, dimmed, until its own detail arrives
  const detail = sampleId && runId ? detailQ.data : null;
  const detailLoading = !!detail && detail.run?.id !== runId;

  useEffect(() => {  // where the 调试台 is, in its address (survives a reload, can be shared)
    if (page !== "studio") return;
    const p = new URLSearchParams();
    if (view !== "studio") p.set("view", view);
    if (view !== "studio" && msPb) p.set("pb", String(msPb));
    if (sampleId && sel.sample) p.set("sample", sampleId);
    if (runId && sel.run) p.set("run", runId);
    if (spread && view === "studio") { p.set("spread", "1"); if (vs) p.set("vs", vs); }
    const q = p.toString();
    history.replaceState(history.state, "", `${location.pathname}${q ? `?${q}` : ""}`);
  }, [page, view, msPb, sampleId, runId, sel.sample, sel.run, spread, vs]);

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
  // 展开: this page beside another finished version of the same sample, newest first (a version counts once, by the run its cell shows)
  const others = mode === "tuning" && sample && run ? (wb?.playbooks || []).slice().sort((a, b) => b.version - a.version)
    .map((p) => runFor(sample!, p.id)).filter((r): r is Run => done(r) && r.id !== run!.id) : [];
  const vsRun = others.find((r) => r.id === vs) ?? prevRun ?? others[0] ?? null;
  const wide = spread && !narrow && page === "studio" && view === "studio" && !!vsRun && pv.data?.configured !== false;
  const vsPv = usePreview(ch.id, wide ? vsRun!.id : null, dirty ? draft : null);
  const vsPreview = vsPv.data?.configured ? vsPv.data : null;
  const cmpRun = wide ? vsRun : cmp ? prevRun : null;
  const cmpQ = usePoll<Compare & { a: { id: string }; b: { id: string } }>(cmpRun && runId ? `/api/compare?a=${cmpRun.id}&b=${runId}` : null, 0);
  const cmpData = cmpRun && cmpQ.data?.a?.id === cmpRun.id && cmpQ.data?.b?.id === runId ? cmpQ.data : null;
  const cmpId = cmpData ? cmpRun?.id ?? null : null;

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
  const pairs = useMemo(() => (cmpData?.pairs || []).flatMap((p) => (p.a && p.b ? [{ a: p.a.id, b: p.b.id }] : [])), [cmpData]);
  const vsSec = useMemo(() => new Map(pairs.flatMap((p) => { const i = secOf.get(p.b); return i === undefined ? [] : [[p.a, i] as const]; })), [pairs, secOf]);
  useLinkedScroll(vsPane, pane, pairs, wide && linked, `${vsPreview?.run_id}:${preview?.run_id}:${vsPv.loading}:${pv.loading}:${pairs.length}`);
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
    try { await fn(...a); } catch (e) {
      flash((e as Error).message);
      if (e instanceof HttpError && e.status === 409) reloadList();  // locked or retuned elsewhere: show it as it is now
    }
  };
  // what spends model quota goes out once: a second click while its request is under way does nothing
  const once = <A extends unknown[]>(key: (...a: A) => string, fn: (...a: A) => Promise<void>) => async (...a: A) => {
    const k = key(...a);
    if (inflight.current.has(k)) return;
    inflight.current.add(k);
    try { await fn(...a); } finally { inflight.current.delete(k); }
  };
  const act: MatrixActions = {
    trial: once((pids: number[], sids?: string[]) => `t:${pids}:${sids || ""}`, guard(async (pids: number[], sids?: string[]) => {
      wbQ.setData(await post<Workbench>(`/api/channels/${ch.id}/trial`, { playbook_ids: pids, source_ids: sids || null }));
      reloadList(); flash("已排进试跑队列");
    })),
    add: async (url: string) => { wbQ.setData(await post<Workbench>(`/api/channels/${ch.id}/samples`, { url })); reloadList(); },
    remove: guard(async (sid: string) => { wbQ.setData(await post<Workbench>(`/api/channels/${ch.id}/samples/${encodeURIComponent(sid)}/remove`)); reloadList(); }),
    clear: guard(async () => { wbQ.setData(await post<Workbench>(`/api/channels/${ch.id}/trial-queue/clear`)); }),
    retry: once((rid: string) => `r:${rid}`, guard(async (rid: string) => { await post(`/api/runs/${encodeURIComponent(rid)}/retry`); wbQ.reload(); })),
  };
  const pick = (sid: string, rid: string) => {
    setSel({ sample: sid, run: rid }); setCmp(false); setWant(null);
    if (sid !== sample?.id) setVs(null);  // the other page stays what was chosen while stepping through this sample's versions
    // an open sheet turns to the version just chosen, so the rules and what they produced stay the same version
    const pid = wb?.samples.find((x) => x.id === sid)?.runs.find((r) => r.id === rid)?.playbook_id;
    if (view !== "studio" && pid) setMsPb(pid);
  };
  // a cell of the table opens below it: the page comes to rest with the bar under the masthead and the three panes in full
  const pickCell = (sid: string, rid: string) => {
    pick(sid, rid);
    const el = bar.current;
    if (!el) return;
    const dy = el.getBoundingClientRect().top - parseFloat(getComputedStyle(el).top);
    if (dy > 1) scrollBy({ top: dy, behavior: smooth() });
    el.focus({ preventScroll: true });
  };
  const adopt = guard(async (sid: string, rid: string | null, pinned: boolean) => {
    await post(`/api/sources/${encodeURIComponent(sid)}/adopt`, { run_id: rid, pinned });
    wbQ.reload();
  });
  // saved since on another device (a12): this save waits for 用我的覆盖 or 放弃我的
  const [overtaken, setOvertaken] = useState(false);
  const saveNotion = guard(async (force = false) => {
    if (!draft) return;
    let v: NotionView;
    try { v = await post<NotionView>(`/api/channels/${ch.id}/notion`, { ...draft, rev: notionQ.data?.rev, force }); }
    catch (e) { if (e instanceof HttpError && e.status === 412) { setOvertaken(true); return; } throw e; }
    setOvertaken(false); notionQ.setData(v); setDraft(null); reloadList();
    flash(v.problems ? `已保存，还有 ${v.problems} 处和 Notion 对不上` : mode === "locked" ? "已保存 · 下一期起按新设置写入" : "已保存");
  });
  const dropNotion = () => { setOvertaken(false); setDraft(null); notionQ.reload(); };
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
  const openMs = (v: "manuscript" | "rules", f?: string) => sheetOpen(() => { setSpread(false); setMsPb(goalPb?.id ?? null); setFocus(f || null); setView(v); });
  const closeMs = () => morph(() => { setView("studio"); setFocus(null); }, ["ch-vt-close"], () => settle(".ch-goal .ch-msx", ".ch-goal"));
  // the Notion page grows to the left over the source and the cards, its right edge and its toggle where they were
  const toggleSpread = () => morph(() => setSpread(!wide), [wide ? "ch-vt-close" : "ch-vt-open", "ch-vt-nt"], keepPlace(pane.current));
  useEffect(() => {
    if (!wide || sheet || target) return;
    const on = (e: KeyboardEvent) => { if (e.key === "Escape" && !e.defaultPrevented && !(e.target as Element).closest?.("input, textarea")) toggleSpread(); };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }); // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------------ what each pane shows
  const version = mode === "tuning" ? (run?.version ? `v${run.version}` : "通用") : activePb ? `v${activePb.version}` : "通用";
  const track: [number, number, number][] = mode === "tuning" && run && !detailLoading ? run.track
    : (detail?.units || []).map((u) => [u.start, u.end, u.learning_priority || 2]);
  const unitMid = new Map((detail?.units || []).map((u) => [u.id, (u.start + u.end) / 2]));
  const flags = mode === "tuning" && run && !detailLoading ? run.flag_at
    : (detail?.flags || []).filter((f) => f.severity !== "INFO" && unitMid.has(f.target_id)).map((f) => unitMid.get(f.target_id) as number);
  const runHead = (s: NonNullable<typeof sample>, r: Run): PaneHead => {
    const adopted = s.adopt.run_id === r.id;
    return {
      version: r.version ? `v${r.version}` : "通用", adopted: adopted ? { pinned: s.adopt.pinned } : null, canAdopt: !adopted, canPin: adopted && !s.adopt.pinned,
      canUnpin: adopted && s.adopt.pinned, onAdopt: () => adopt(s.id, r.id, true), onPin: () => adopt(s.id, r.id, true),
      onUnpin: () => adopt(s.id, null, false), cmpVersion: null, cmp: false, onCmp() {},
      link: { href: `/issue/${encodeURIComponent(s.id)}?run=${encodeURIComponent(r.id)}`, text: "对照视图", external: false },
    };
  };
  const head: PaneHead | null = mode === "tuning" && sample && run ? {
    ...runHead(sample, run), cmpVersion: (wide ? vsRun?.version : prevRun?.version) ?? null, cmp, onCmp: () => setCmp(!cmp),
    spread: !narrow && vsRun && pv.data?.configured ? { on: wide, toggle: toggleSpread } : undefined,
  } : issue ? {
    version, adopted: null, canAdopt: false, canPin: false, canUnpin: false, onAdopt() {}, onPin() {}, onUnpin() {},
    cmpVersion: null, cmp: false, onCmp() {},
    link: issue.notion_url ? { href: issue.notion_url, text: "在 Notion 打开", external: true } : { href: `/issue/${encodeURIComponent(issue.id)}`, text: "对照视图", external: false },
  } : null;
  const notionEmpty = pv.data && !pv.data.configured
    ? <div className="ch-noconf"><p>这个频道还没有 Notion 目标。</p><button className="ch-btn sm solid" onClick={() => setTarget(true)}>选一个数据库</button></div>
    : !runId ? <div className="ch-noconf"><p>{mode === "tuning" ? "选一格跑完的样片看它写进 Notion 的样子。" : "还没有可以预览的一期。"}</p></div> : null;
  const testCopy = !!((notionQ.data as any)?.test_copy ?? ch.notion.test_copy);
  // wide: source | cards | Notion; narrower: cards | one preview with a switch; phones: the preview above the cards
  const switcher = compact ? <Seg small label="预览" value={tab} onChange={setTab} options={[{ value: "notion", label: "Notion 页面" }, { value: "source", label: "原片" }]} /> : null;
  const notionPane = (
    <NotionPane preview={preview} error={pv.error} loading={pv.loading} head={head} cmp={cmpData} secOf={secOf} propKey={propKey}
      titleKey={titleKey} fieldOf={fieldOf} paneRef={pane} empty={notionEmpty} switcher={wide ? null : switcher} />
  );
  const vsPage = wide && sample && (
    <NotionPane other preview={vsPreview} error={vsPv.error} loading={vsPv.loading} cmp={cmpData} secOf={vsSec} propKey={propKey}
      titleKey={titleKey} fieldOf={fieldOf} paneRef={vsPane}
      head={{ ...runHead(sample, vsRun!), extra: <button className={`ch-phb${linked ? " on" : ""}`} aria-pressed={linked} onClick={() => setLinked(!linked)}>联动</button> }}
      picker={<span className="ch-vsv" role="group" aria-label="对比版本">{others.map((r) => (
        <button key={r.id} className={`ch-vsb${r.id === vsRun!.id ? " on" : ""}`} aria-pressed={r.id === vsRun!.id} onClick={() => setVs(r.id)}>v{r.version}</button>
      ))}</span>} />
  );
  const sourcePane = (
    <SourcePane detail={detail} loading={detailLoading || (!detail && !!(sampleId && runId))} channelName={ch.name} track={track} flags={flags} version={version} cmp={cmpData}
      prevVersion={prevRun?.version ?? null} secOf={secOf} badge={badge} keys={fieldKeys} onJump={jump} switcher={switcher} />
  );
  const turn = narrow && want && wb ? <RunPending wb={wb} want={want} act={act} switcher={switcher} /> : tab === "source" ? sourcePane : notionPane;
  const lockPb = draftPb || activePb;
  const hasBar = mode === "tuning" && !narrow && !!wb && !!sample && !!run;
  // unsaved Notion settings stay with the channel, and the bar that saves them, on the 看板 too
  const dock = useLeaving(dirty && (page === "board" || view === "studio"));
  const toastOut = useLeaving(toast);
  const tabs = mode === "locked" ? (
    <Seg small label="频道页面" value={page} onChange={(p) => navigate(`/channels/${encodeURIComponent(ch.id)}/${p}`, { quiet: true })}
      options={[{ value: "board", label: "看板" }, { value: "studio", label: "调试台" }]} />
  ) : null;

  return (
    <div className="chp">
      <Header ch={ch} list={list} alerts={alerts} testCopy={testCopy} lockVersion={mode === "tuning" ? lockPb?.version ?? null : null}
        onLock={() => setSheet(true)} onRetune={retune} reload={reloadList} tabs={tabs} />
      {page === "board" ? <Board ch={ch} mode={mode} onSetIngest={setIngest} flash={flash} /> : <>
      {mode === "tuning" && (wb
        ? <Matrix wb={wb} ch={ch} sel={{ sample: sample?.id ?? null, run: run?.id ?? null }} cmp={cmpId} act={act}
            want={narrow ? want : null} onWant={setWant} onPick={pickCell} />
        : <section className="ch-tb loading" aria-busy="true" />)}
      {hasBar && <Current wb={wb!} s={sample!} run={run!} cmp={cmpId} onPick={pick} act={act} barRef={bar} />}
      {mode === "locked" && <LockedBar issues={issues} selected={issue?.id ?? null} onPick={(i) => setSel({ sample: i.id, run: i.current_run_id })} />}
      {mode === "discovery" && <DiscoveryBar issue={issue} />}
      <div className={`ch-studio${view !== "studio" ? " ms" : ""}${hasBar ? " hc" : ""}${wide ? " wide" : ""}`} ref={studio}>
        {wide ? vsPage : view === "studio" ? (
          <>
            {!compact ? sourcePane : narrow ? turn : null}
            <div className="ch-cards" aria-label="设置">
              <GoalCard mode={mode} ch={ch} pb={goalPb} ann={goalAnn}
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
              <IntakeCard ch={ch} mode={mode} onSet={setIngest}
                onBoard={mode === "locked" ? () => navigate(`/channels/${encodeURIComponent(ch.id)}/board`, { quiet: true }) : undefined} />
            </div>
          </>
        ) : (
          <Manuscript ch={ch} mode={mode} pbId={msPb ?? goalPb?.id ?? null} setPbId={setMsPb} view={view} setView={setView} focus={focus}
            seed={annQ.data} onClose={closeMs} onRetune={() => retune().catch((e) => flash(e.message))} onSave={savePrompt}
            onRecompile={recompile} initialText={initialText} />
        )}
        {wide || view !== "studio" || !compact ? notionPane : !narrow ? turn : null}
        <Wires host={studio} />
      </div>
      </>}
      {dock.shown && (
        <div className={`ch-dock${dock.leaving ? " leaving" : ""}`} role="region" aria-label="保存" inert={dock.leaving}>
          <span className="pulse" aria-hidden />
          {overtaken ? <>
            <span>这个频道的 Notion 设置刚在别处改过</span>
            <button className="ch-btn sm" onClick={dropNotion}>放弃我的</button>
            <button className="ch-btn sm solid" onClick={() => saveNotion(true)}>用我的覆盖</button>
          </> : <>
            <span>Notion 设置有改动{mode === "locked" ? " · 下一期起生效" : ""}</span>
            <button className="ch-btn sm" onClick={dropNotion}>放弃</button>
            <button className="ch-btn sm solid" onClick={() => saveNotion()}>保存</button>
          </>}
        </div>
      )}
      {sheet && lockPb && (
        <LockSheet cid={ch.id} defaultPb={lockPb.id} onClose={() => setSheet(false)}
          onLocked={(r) => { setSheet(false); reloadList(); flash(`已定稿 v${r.version} · 写入 ${r.write} 期${r.backfill ? ` · 补录 ${r.backfill} 期` : ""}${r.ingest?.enabled ? " · 自动收录已开启" : ""}`, true); }}
          onConflict={(c) => { setSheet(false); openMs("manuscript", `cl:${c}`); }} onRetarget={() => { setSheet(false); setTarget(true); }}
          onTrial={async (pid) => { await act.trial([pid]); }} />
      )}
      {target && (
        <TargetPicker cid={ch.id} current={nv?.database.id || null} onClose={() => setTarget(false)}
          onDone={(v) => {
            setTarget(false); notionQ.setData(v); setDraft(null); reloadList();
            flash(["已换库", v.dropped?.length && `新库里没有：${v.dropped.join("、")}`, v.repick?.length && `需要重选：${v.repick.join("、")}`].filter(Boolean).join(" · "));
          }} />
      )}
      {toastOut.shown && <Toast text={toastOut.shown.text} seal={toastOut.shown.seal} leaving={toastOut.leaving} />}
    </div>
  );
}
