import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { post } from "../api";
import { boxLines, cookieState, type Line, MAGPIE_DOWN, MAIN_STEPS, quotaLefts, STEP } from "../box";
import { openAgyLogin } from "../components/AgyLogin";
import { BackfillPanel } from "../components/BackfillPanel";
import { Cmd } from "../components/Cmd";
import { CookieCloudSheet } from "../components/CookieCloud";
import { AsrForm, DiskRows, NotionRows, ParallelRows, Row, type Save, Stepper, TelegramForm, useKnobs } from "../components/Knobs";
import { type Left, lostRoutes, NO_ROUTE, RouteCells, RoutePicker } from "../components/Routes";
import { Fault, Stale, Unroll } from "../components/States";
import { useLeaving, useMedia, usePoll } from "../hooks";
import { returning, useRestoreScroll } from "../router";
import { ago, bytes, errorLabel, smooth } from "../util";

/* 设置: everything about the box that can be set, and how what it depends on is doing (design §R6-7). A control saves by
   itself; a form with text in it saves when asked to. /settings#<section> goes straight to one; #cc opens CookieCloud. */

const SECTIONS: { k: string; name: string }[][] = [
  [{ k: "models", name: "模型分工" }, { k: "retry", name: "自动重试" }, { k: "slots", name: "并行" }, { k: "backfill", name: "补录" }, { k: "disk", name: "磁盘" }],
  [{ k: "agy", name: "Gemini" }, { k: "magpie", name: "Magpie" }, { k: "youtube", name: "YouTube" }, { k: "notion", name: "Notion" }, { k: "asr", name: "GPU 转写" }, { k: "notify", name: "通知" }],
];
const ALL = SECTIONS.flat();
const IDS = ALL.map((s) => s.k);
const NARROW = "(max-width: 900px)";  // with useChromeAutoHide in App.tsx and .st-nav
const SSH = "ssh -N -L 6080:127.0.0.1:6080 ovh-investment";

/** Which section the page is at, and going to one. On the way the list stays on where it is going; the section says so on arrival. */
function useSections(ready: boolean, nav: React.RefObject<HTMLElement | null>) {
  const [at, setAt] = useState(IDS[0]);
  const going = useRef<HTMLElement | null>(null);
  const still = useRef<number | undefined>(undefined);
  const back = useRef(returning()).current;
  const land = useCallback(() => {
    const el = going.current;
    going.current = null;
    if (!el) return;
    el.classList.remove("flash");
    void el.offsetWidth;  // the same section twice: from the start again
    el.classList.add("flash");
    window.setTimeout(() => el.classList.remove("flash"), 1500);
  }, []);
  const go = useCallback((id: string, glide = true) => {
    const el = document.getElementById(id), head = document.querySelector<HTMLElement>(".masthead");
    if (!el || !head) return;
    setAt(id);
    going.current = el;
    history.replaceState(history.state, "", `#${id}`);
    const narrow = matchMedia(NARROW).matches;
    const bar = narrow ? nav.current?.offsetHeight ?? 0 : 0, gap = narrow ? 12 : 24;
    const y = el.getBoundingClientRect().top + scrollY - bar - gap;
    // on a phone the masthead is gone on the way down and back on the way up: land clear of it whichever it will be
    const safe = parseFloat(getComputedStyle(head).paddingTop) || 0;
    const chrome = narrow && y - safe > scrollY && y - safe >= 120 ? safe : head.offsetHeight;
    window.scrollTo({ top: Math.max(0, y - chrome), behavior: glide ? smooth() : "auto" });
    el.focus({ preventScroll: true });
    clearTimeout(still.current);
    still.current = window.setTimeout(land, 160);
  }, [land, nav]);

  useEffect(() => {
    if (!ready) return;
    let raf = 0;
    const read = () => {
      raf = 0;
      const line = innerHeight * 0.3;
      let now = IDS[0];
      for (const id of IDS) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top <= line) now = id;
      }
      if (innerHeight + scrollY >= document.documentElement.scrollHeight - 2) now = IDS[IDS.length - 1];
      setAt(now);
    };
    const on = () => {
      if (going.current) { clearTimeout(still.current); still.current = window.setTimeout(land, 160); }
      else if (!raf) raf = requestAnimationFrame(read);
    };
    const takenOver = () => { if (going.current) { clearTimeout(still.current); land(); } };
    const toHash = () => { if (IDS.includes(location.hash.slice(1))) go(location.hash.slice(1)); };
    addEventListener("scroll", on, { passive: true });
    addEventListener("resize", on);
    addEventListener("wheel", takenOver, { passive: true });
    addEventListener("touchstart", takenOver, { passive: true });
    addEventListener("hashchange", toHash);
    const hash = location.hash === "#cc" ? "youtube" : location.hash.slice(1);
    // gone back to, the page is where the reader left it (useRestoreScroll); opened or reloaded, where the address says
    if (IDS.includes(hash) && !back) go(hash, false);
    else read();
    return () => {
      removeEventListener("scroll", on);
      removeEventListener("resize", on);
      removeEventListener("wheel", takenOver);
      removeEventListener("touchstart", takenOver);
      removeEventListener("hashchange", toHash);
      cancelAnimationFrame(raf);
      clearTimeout(still.current);
    };
  }, [ready, go, land, back]);

  // a list that scrolls sideways (phones) keeps the current section in view
  useEffect(() => {
    const n = nav.current?.firstElementChild, a = n?.querySelector<HTMLElement>('a[aria-current]');
    if (!n || !a || n.scrollWidth <= n.clientWidth) return;
    n.scrollTo({ left: a.offsetLeft - (n.clientWidth - a.offsetWidth) / 2, behavior: smooth() });
  }, [at, nav, ready]);
  return { at, go };
}

function Sec({ id, title, line, children }: { id: string; title: string; line?: Line; children: React.ReactNode }) {
  return (
    <section id={id} className="st-sec" tabIndex={-1} aria-labelledby={`${id}-h`}>
      <div className="rule-title st-h">
        <h2 id={`${id}-h`}>{title}</h2>
        {line && <span className={`st-state ${line.lamp}`}><i className={`lamp ${line.lamp}`} aria-hidden />{line.value}</span>}
      </div>
      {children}
    </section>
  );
}

const RETRY_GROUPS = [
  { title: "用完算失败", kinds: ["CONTENT_SAFETY", "CONTRACT", "MODALITY_ROUTING_DEVIATION", "MEDIA_READ_FAILED", "PROVIDER_FAILURE", "PROVIDER_OUTPUT_LIMIT"] },
  { title: "用完转入等待", kinds: ["PROVIDER_TRANSIENT", "PROVIDER_TIMEOUT"] },
];
const times = (n: number) => (n ? `${n} 次` : "不重试");

function RetryRow({ kind, data, save }: { kind: string; data: any; save: Save }) {
  const n: number = data.knobs.retry_counts[kind] ?? 0, d: number = data.retry_defaults[kind] ?? 0, s = data.retries?.[kind];
  const name = errorLabel(kind);
  const drop = useRef<((to?: number) => void) | null>(null);  // a count clicked to and not saved yet would undo 默认
  const reset = async () => {
    drop.current?.(d);
    if (!await save({ retry_count: { [kind]: null } })) drop.current?.();
  };
  return (
    <div className={`st-row${kind === "CONTENT_SAFETY" ? " st-joined" : ""}`}>
      <div className="st-k st-retry">
        <span>{name}</span>
        {s?.tried > 0 && <span className="st-note" title="近 30 天">重试 {s.tried} 次 · 救回 {s.rescued}</span>}
        {n !== d && <button type="button" className="st-dflt" aria-label={`${name}恢复默认：${times(d)}`} onClick={reset}>默认{d ? ` ${d}` : "不重试"}</button>}
      </div>
      <div className="st-v"><Stepper value={n} lo={0} hi={data.retry_max} fmt={times} save={save} label={name} body={(v) => ({ retry_count: { [kind]: v } })} drop={drop} /></div>
    </div>
  );
}

/** The safety filter's last resort: stored, or where 知识 goes (knobs.fallback_route); picking that one goes back to following
 *  知识. A stage on the same model has no other to go to; one that watches video, none that cannot (providers.safety_fallback). */
function Fallback({ data, models, lefts, save }: { data: any; models: any[]; lefts: Record<string, Left | null>; save: Save }) {
  const stored: string | null = data.knobs.safety_fallback;
  const routes = { default: data.providers?.default || "agy", stages: data.providers?.stages || {} };
  const follow: string = routes.stages.knowledge || routes.default;
  const [pick, setPick] = useState<string | null>(null);
  useEffect(() => setPick(null), [stored]);
  const value = pick ?? (stored || follow);
  const blind = models.find((m) => m.id === value)?.watches === false;
  const all = MAIN_STEPS.every((s) => (routes.stages[s] || routes.default) === value);
  return (
    <div className="st-routes st-fallback">
      <RoutePicker value={value} models={models} video={false} name="还被拦就换" bad={false} lefts={lefts} none
        note={value === NO_ROUTE ? undefined : all ? "各工序都在用它，不换" : blind ? "理解、边界要看视频，不换" : undefined}
        onPick={async (r) => { setPick(r); if (!await save({ safety_fallback: r === follow ? null : r })) setPick(null); }} />
    </div>
  );
}

function Disk({ data, save }: { data: any; save: Save }) {
  const d = data.disk;
  const pc = (x: number) => `${Math.max(0.4, (x / d.total) * 100)}%`;
  const other = Math.max(0, d.total - d.free - d.media - d.runs - d.packages);
  return (
    <>
      <div className="st-block">
        <div className="diskbar" role="img" aria-label={`媒体 ${bytes(d.media)}，工作区 ${bytes(d.runs)}，素材包 ${bytes(d.packages)}`}>
          <i className="media" style={{ width: pc(d.media) }} />
          <i className="work" style={{ width: pc(d.runs) }} />
          <i className="pk" style={{ width: pc(d.packages) }} />
          <i className="other" style={{ width: pc(other) }} />
        </div>
        <div className="legend">
          <span className="media">媒体 {bytes(d.media)}</span>
          <span className="work">工作区 {bytes(d.runs)}</span>
          <span className="pk">素材包 {bytes(d.packages)}</span>
        </div>
      </div>
      <DiskRows kn={data.knobs} save={save} />
    </>
  );
}

function Gemini({ data }: { data: any }) {
  const q = data.quota?.agy, media = data.agy_media;
  const version = media?.version || q?.version;
  return (
    <>
      {version && <Row name="版本">agy {version}</Row>}
      {media && <Row name="读视频自检"><span className={media.ok ? undefined : "zhu"}>{media.ok ? "通过" : "没通过"}{media.checked_at ? ` · ${ago(media.checked_at)}` : ""}</span></Row>}
      {q?.error === "AGY_AUTH_REQUIRED" && <div className="st-acts"><button type="button" className="btn zhu sm" onClick={openAgyLogin}>登录 agy</button></div>}
    </>
  );
}

function Magpie({ data, lamp }: { data: any; lamp: string }) {
  const m = data.magpie || {}, st = m.state || {}, r = m.report;
  return (
    <>
      {st.error && <Row name="状态"><span className={lamp === "bad" ? "zhu" : undefined}>{MAGPIE_DOWN[st.error] || st.error}{st.checked_at ? ` · ${ago(st.checked_at)}` : ""}</span></Row>}
      {st.key?.name && <Row name="Key">「{st.key.name}」{st.key.limited ? " · 受限" : ""}{st.key.read_only ? " · 只读" : ""}</Row>}
      {(st.models?.length > 0 || st.version) && <Row name="模型">{[st.models?.length ? `${st.models.length} 个` : "", st.version].filter(Boolean).join(" · ")}</Row>}
      {r && <Row name="Gemini 调用上报"><span className={r.ok ? undefined : "zhu"}>{r.ok ? `${r.total || 0} 次 · ${ago(r.at)}` : `报不上去：${MAGPIE_DOWN[r.error] || r.error}`}</span></Row>}
      {m.url && (
        <div className="st-acts">
          <a className="btn sm" href={`${m.url}/?view=providers`} target="_blank" rel="noreferrer">供应商 ↗</a>
          <a className="btn sm" href={`${m.url}/?view=gateway`} target="_blank" rel="noreferrer">网关 ↗</a>
        </div>
      )}
    </>
  );
}

function Youtube({ data, lamp, onCc }: { data: any; lamp: string; onCc: () => void }) {
  const y = data.youtube;
  const narrow = useMedia(NARROW);
  const { cc, read, down, kept } = cookieState(y);
  const need = data.alerts.some((a: any) => a.key === "YOUTUBE_AUTH" && !a.resolved_at);
  const vt = y.video_test?.verdict ? y.video_test : null;
  const startBrowser = async () => {
    const x = await post("/api/youtube/remote-browser");
    if (x.ok) window.open(x.url, "_blank"); else alert(x.output);
  };
  return (
    <>
      <Row name="来源">{cc?.configured ? "CookieCloud" : "服务器 Chromium"}</Row>
      {down && (
        <p className={`st-fail${lamp === "bad" ? "" : " mild"}`}>
          {kept && !need && <>下载在用上一份登录态，{ago(kept)}读到的。</>}
          {y.fix?.[read.code] || read.code}（错误码 {read.code}{read.code === "ip_not_allowed" && read.detail ? ` · ${read.detail}` : ""}{read.at && ` · ${ago(read.at)}`}）
          {!kept && "这段时间的下载改用服务器上的 Chromium。"}
        </p>
      )}
      {cc?.configured && read?.ok && <Row name="上次读取">{ago(read.at)} · {read.count} 条</Row>}
      {cc?.configured && vt && <Row name="视频测试">{vt.ok ? "通过" : "没通过"}{vt.video?.title ? ` · 《${vt.video.title}》` : ""}{vt.at ? ` · ${ago(vt.at)}` : ""}</Row>}
      <div className="st-acts"><button type="button" className="btn sm" onClick={onCc}>{cc?.configured ? "配置与测试" : "配置 CookieCloud"}</button></div>
      {!cc?.configured && !narrow && y.profile && (
        <details className="fold mini st-alt">
          <summary><i className="plus" aria-hidden /><span>在服务器 Chromium 里登录</span></summary>
          <Cmd text={SSH} />
          <div className="st-acts"><button type="button" className="btn sm" onClick={startBrowser}>启动远程登录屏幕</button></div>
        </details>
      )}
    </>
  );
}

export default function Settings() {
  const { data, error, loadedAt, reload } = usePoll<any>("/api/settings", 10000);
  const { save, said, say } = useKnobs(reload);
  const told = useLeaving(said);
  const [cc, setCc] = useState(() => location.hash === "#cc");
  const nav = useRef<HTMLElement>(null);
  const { at, go } = useSections(!!data, nav);
  useRestoreScroll(!!data);
  useEffect(() => {
    addEventListener("az:refresh", reload);
    return () => removeEventListener("az:refresh", reload);
  }, [reload]);
  useEffect(() => {  // the address changing under an open page
    const on = () => { if (location.hash === "#cc") { go("youtube"); setCc(true); } };
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, [go]);
  const closeCc = useCallback(() => {
    setCc(false);
    history.replaceState(history.state, "", "#youtube");
  }, []);
  if (!data) return <main className="page settings">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;

  const kn = data.knobs;
  const models: any[] = [...(data.agy_models || []), ...(data.magpie?.state?.models || [])];
  const routes = { default: data.providers?.default || "agy", stages: data.providers?.stages || {} };
  const missing: string[] = data.magpie?.missing || [];
  const lost = lostRoutes(MAIN_STEPS, routes, models, missing);
  const lines: Record<string, Line | undefined> = { ...boxLines(data), models: lost ? { lamp: "bad", value: `${lost} 处要换` } : undefined };
  const bad = ALL.filter((s) => lines[s.k]?.lamp === "bad");
  const lefts = quotaLefts(data);
  const jump = (id: string) => (e: React.MouseEvent) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    go(id);
  };
  return (
    <main className="page settings">
      <Stale error={error} loadedAt={loadedAt} />
      <header className="pagehead">
        <h1>设置</h1>
        {bad.length > 0 && (
          <p className="ph-status bad">
            <i className="lamp bad" aria-hidden />
            <span className="st-bad">{bad.map((s) => <a key={s.k} href={`#${s.k}`} onClick={jump(s.k)}>{s.name} {lines[s.k]!.value}</a>)}</span>
          </p>
        )}
      </header>
      <div className="st-body">
        <nav ref={nav} className="st-nav" aria-label="设置分区">
          <div className="st-navs">
            {SECTIONS.map((g, i) => (
              <ol key={i}>
                {g.map((s) => {
                  const lamp = lines[s.k]?.lamp;
                  return (
                    <li key={s.k}>
                      <a href={`#${s.k}`} aria-current={at === s.k ? "location" : undefined} onClick={jump(s.k)}>
                        {s.name}{(lamp === "bad" || lamp === "warn") && <i className={`lamp ${lamp}`} aria-hidden />}
                      </a>
                    </li>
                  );
                })}
              </ol>
            ))}
          </div>
        </nav>
        <div className="st-main">
          <Sec id="models" title="模型分工" line={lines.models}>
            <div className="st-routes">
              <RouteCells kn={kn} steps={MAIN_STEPS} label={STEP} save={save} models={models} lefts={lefts} routes={routes} missing={missing} />
            </div>
          </Sec>
          <Sec id="retry" title="自动重试">
            {RETRY_GROUPS.map((g) => (
              <Fragment key={g.title}>
                <h3 className="st-group">{g.title}</h3>
                {g.kinds.map((k) => (
                  <Fragment key={k}>
                    <RetryRow kind={k} data={data} save={save} />
                    {k === "CONTENT_SAFETY" && <Fallback data={data} models={models} lefts={lefts} save={save} />}
                  </Fragment>
                ))}
              </Fragment>
            ))}
          </Sec>
          <Sec id="slots" title="并行"><ParallelRows kn={kn} save={save} /></Sec>
          <Sec id="backfill" title="补录"><BackfillPanel kn={kn} view={data.backfill} save={save} /></Sec>
          <Sec id="disk" title="磁盘" line={lines.disk}><Disk data={data} save={save} /></Sec>
          <Sec id="agy" title="Gemini" line={lines.agy}><Gemini data={data} /></Sec>
          <Sec id="magpie" title="Magpie" line={lines.magpie}><Magpie data={data} lamp={lines.magpie!.lamp} /></Sec>
          <Sec id="youtube" title="YouTube" line={lines.youtube}><Youtube data={data} lamp={lines.youtube!.lamp} onCc={() => setCc(true)} /></Sec>
          <Sec id="notion" title="Notion" line={lines.notion}><NotionRows n={data.notion} reload={reload} say={say} /></Sec>
          <Sec id="asr" title="GPU 转写" line={lines.asr}><AsrForm a={data.asr} reload={reload} say={say} /></Sec>
          <Sec id="notify" title="通知" line={lines.notify}><TelegramForm tg={data.telegram} save={save} reload={reload} say={say} /></Sec>
        </div>
      </div>
      <div role={told.shown?.bad ? "alert" : "status"}>
        {told.shown && <div className={`ch-toast${told.shown.bad ? " bad" : ""}${told.leaving ? " leaving" : ""}`}>{told.shown.text}</div>}
      </div>
      {cc && <CookieCloudSheet yt={data.youtube} onClose={closeCc} reload={reload} />}
    </main>
  );
}
