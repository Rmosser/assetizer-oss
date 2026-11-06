import { Fragment, useEffect, useMemo, useState } from "react";
import { media, post, srcSet } from "../api";
import { IssueTitle, issueHeadline, originalTitle, typeset, Words } from "../cjk";
import { agyAlert, openAgyLogin } from "../components/AgyLogin";
import { about, big, HBars, Heat, hrs, Kpi, ymd } from "../components/Figures";
import { Fold } from "../components/Fold";
import { Empty, Fault, Stale, Unroll } from "../components/States";
import { useMedia, usePoll } from "../hooks";
import { linkProps, useRestoreScroll } from "../router";
import { cnDate, cnDay, cnMonth, cnNum, ERRORS, HELD, mergeAlerts, parseDay, PRODUCTION, STOPS, UNAVAILABLE, WAIT } from "../util";

type Stage = { name: string; label: string; status: string; started_at?: string };

export function PressLine({ run }: { run: any }) {
  const [reveal, setReveal] = useState(false);
  const stages: Stage[] = run.stages.filter((s: Stage) => s.name !== "CLEANUP");
  const idx = Math.max(0, stages.findIndex((s) => s.status === "RUNNING" || s.status === "PENDING"));
  const doneCount = stages.filter((s) => ["DONE", "WARN", "SKIPPED"].includes(s.status)).length;
  const pct = Math.min(100, (doneCount / (stages.length - 1)) * 100);
  const current = stages.find((s) => s.status === "RUNNING") || stages[idx];
  const at = stages.indexOf(current) + 1;
  return (
    <section className="press" aria-label="正在付印">
      <div className="press-head">
        <span className="label">正在付印</span>
        <h3><IssueTitle title={run.source?.title || run.source_id} /></h3>
        <span className="eta">{current?.label}{run.eta_seconds ? ` · ${about(run.eta_seconds)}后可读` : ""}</span>
      </div>
      <div className={`rail${reveal ? " reveal" : ""}`} role="progressbar" aria-label="付印进度"
        aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-valuetext={`第 ${at}/${stages.length} 站：${current?.label || ""}`}
        onClick={() => { setReveal(true); setTimeout(() => setReveal(false), 3000); }}>
        <div className="rail-line" />
        <div className="rail-fill" style={{ width: `${pct}%` }} />
        {stages.map((s, i) => (
          <div key={s.name} className={`rail-stop ${s.status === "RUNNING" ? "run" : ["DONE", "SKIPPED"].includes(s.status) ? "done" : s.status === "WARN" ? "warn" : ""} ${i === 0 ? "first" : ""} ${i === stages.length - 1 ? "last" : ""}`}
            style={{ left: `${(i / (stages.length - 1)) * 100}%` }}>
            <i />
            <span>{s.label}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

const headOf = (it: any) => issueHeadline(it.headline, it.musts?.[0]?.title, it.title);
const producing = (it: any) => ["QUEUED", "RUNNING", "WAITING", "INTERRUPTED"].includes(it.production) && !it.units;
/** nothing was made and nothing will be: too short to take, or a video no login opens, put away */
const dropped = (it: any) => it.production === "CANCELLED" && it.units == null;
const issueHref = (it: any, hash = "") => `/issue/${encodeURIComponent(it.id)}${hash}`;

/** Quiet when all is well (the issue is in Notion); speaks only for samples, production and delivery trouble. */
function State({ it }: { it: any }) {
  if (producing(it)) return <span className="st wip">{it.production === "WAITING" ? "等待中" : it.production === "RUNNING" ? "付印中" : "排队中"}</span>;
  if (it.sample) return <span className="st" title="调试中频道的样片，定稿后写入 Notion">样片</span>;
  if (it.delivery === "FAILED") return <span className="st pi">交付失败</span>;
  if (it.delivery === "REGISTERED") return <span className="st" title="Notion 里已有这个视频的页面，没有重复写入">已登记</span>;
  if (it.delivery === "HELD" && HELD[it.delivery_reason]) return <span className="st pi">{HELD[it.delivery_reason]}</span>;
  if (it.delivery !== "DELIVERED") return <span className="st">未入 Notion</span>;
  return null;
}

/** The newest issue, set like a feature page: cover, title, the three must-reads, and where to read it. */
function Feature({ it }: { it: any }) {
  return (
    <section className="feature" aria-label="本期">
      <a className="feature-cover" {...linkProps(issueHref(it))} tabIndex={-1} aria-hidden>
        {it.cover ? <img src={media(it.cover, 1280)} srcSet={srcSet(it.cover, [640, 960, 1280])} sizes="(max-width: 640px) 100vw, 58vw" alt="" fetchPriority="high" /> : <span className="ph">刊</span>}
      </a>
      <div className="feature-text">
        <div className="kicker">{it.channel || "未识别频道"} · 第{cnNum(it.no)}期{it.published_at ? ` · ${cnDay(it.published_at)}` : ""}</div>
        <h2 className="feature-title"><a {...linkProps(issueHref(it))}><Words>{headOf(it)}</Words></a></h2>
        {originalTitle(it.title) !== headOf(it) && <p className="dek"><Words>{originalTitle(it.title)}</Words></p>}
        {it.units != null ? (
          <p className="feature-stats"><span className="ph"><span className="num">{Math.round((it.duration || 0) / 60)}</span> 分钟</span> · <span className="ph"><span className="num">{it.units}</span> 单元（必学 <span className="num">{it.must}</span>）</span> · <span className="ph"><span className="num">{it.clips}</span> 段片段</span></p>
        ) : <p className="feature-stats">尚未成刊</p>}
        {it.musts?.length > 0 && (
          <div className="musts">
            <span className="label">必学</span>
            <ol>
              {it.musts.map((u: any) => (
                <li key={u.id}><a {...linkProps(issueHref(it, `#u-${u.id}`))}><Words>{u.title}</Words></a></li>
              ))}
            </ol>
          </div>
        )}
        <div className="feature-foot">
          {it.notion_url ? (
            <a className="go" href={it.notion_url} target="_blank" rel="noreferrer">在 Notion 阅读 <span className="arrow">↗</span></a>
          ) : <span className="muted">{it.sample ? "样片" : "未入 Notion"}</span>}
          <a className="go quiet" {...linkProps(issueHref(it))}>对照原视频 <span className="arrow">→</span></a>
        </div>
      </div>
    </section>
  );
}

const SIXTY_DAYS = 60 * 86400 * 1000;

/** One issue in a list. `own`: the list is one channel's (its 看板), so the channel goes unsaid. */
export function Row({ it, own }: { it: any; own?: boolean }) {
  // issues older than sixty days collapse into a single 44px line: no cover, title, date, state
  const d = parseDay(it.published_at) || parseDay(it.updated_at);
  const compact = !!d && Date.now() - d.getTime() > SIXTY_DAYS;
  return (
    <li>
      <a className={`toc-row${compact ? " compact" : ""}`} {...linkProps(issueHref(it))}>
        <div className="toc-no"><span className="num">{it.no == null ? "—" : String(it.no).padStart(3, "0")}</span></div>
        <div className="cover">
          {it.cover ? <img src={media(it.cover, 320)} srcSet={srcSet(it.cover, [320, 640])} sizes="120px" alt="" loading="lazy" /> : <span className="ph">刊</span>}
        </div>
        <div className="toc-title">
          <h3><Words>{headOf(it)}</Words></h3>
          <div className="meta">
            {/* the dot sits outside the unbreakable phrases (a space inside one is dropped) and never starts a line */}
            {(compact ? [cnDay(it.published_at)] : [!own && (it.channel || "未识别频道"), cnDay(it.published_at), `${Math.round((it.duration || 0) / 60)} 分钟`, it.units != null && `${it.units} 单元`])
              .filter(Boolean).map((x, i) => <Fragment key={i}>{i > 0 && " · "}<span className="ph">{x}</span></Fragment>)}
          </div>
        </div>
        <div className="toc-state"><State it={it} /></div>
      </a>
    </li>
  );
}

const FILTERS: [string, string, (it: any) => boolean][] = [["all", "全部", () => true]];

function Archive({ issues }: { issues: any[] }) {
  const [f, setF] = useState("all");
  const channels = useMemo(() => [...new Set(issues.map((i) => i.channel).filter(Boolean))] as string[], [issues]);
  const pred = FILTERS.find(([k]) => k === f)?.[2] || ((it: any) => it.channel === f);
  const shown = issues.filter(pred);
  const groups: [string, any[]][] = [];
  for (const it of shown) {
    const d = parseDay(it.published_at) || parseDay(it.updated_at);
    const key = d ? cnMonth(d) : "未注明日期";
    const g = groups.find(([k]) => k === key);
    if (g) g[1].push(it); else groups.push([key, [it]]);
  }
  return (
    <>
      <div className="rule-title">
        <h2>往期</h2>
        {issues.length >= 4 && channels.length > 1 && (
          <div className="chips" role="group" aria-label="筛选">
            {FILTERS.map(([k, label]) => <button key={k} className={f === k ? "on" : ""} onClick={() => setF(k)}>{label}</button>)}
            {channels.map((c) => <button key={c} className={f === c ? "on" : ""} onClick={() => setF(c)}>{c}</button>)}
          </div>
        )}
      </div>
      {shown.length === 0 ? <p className="muted pad">没有符合条件的期次。</p> : groups.map(([k, list]) => (
        <section key={k} className="month">
          <h4 className="month-head">{k}</h4>
          <ol className="toc">{list.map((it) => <Row key={`${it.id}-${it.no}`} it={it} />)}</ol>
        </section>
      ))}
    </>
  );
}

/** QA only: ?mock=50 fills the archive so the long state can be inspected. */
function mockIssues(issues: any[]): any[] {
  const n = +(new URLSearchParams(location.search).get("mock") || 0);
  if (!n || !issues.length) return issues;
  const base = issues[0];
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(2026, 8, 8 - i * 3);
    return { ...base, id: `${base.id}`, no: n - i, tuning: i % 7 === 2, sample: i % 7 === 2, delivery: i % 9 === 4 ? "FAILED" : base.delivery,
      published_at: `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}` };
  });
}

const WEEK_MS = 7 * 86400 * 1000;
const at = (s?: string | null) => (s ? new Date(s).getTime() : NaN);
/** when an issue counts as published: the video's date, else when it was made */
const when = (it: any) => (parseDay(it.published_at) || parseDay(it.ready_at) || parseDay(it.updated_at))?.getTime() ?? 0;

/** The edition's line: today's date, and what the journal holds. */
function Dateline({ ready }: { ready: any[] }) {
  const d = cnDate();
  const now = Date.now();
  const fresh = ready.filter((i) => now - at(i.ready_at) < WEEK_MS);
  const units = ready.reduce((a, i) => a + (i.units || 0), 0);
  const must = ready.reduce((a, i) => a + (i.must || 0), 0);
  const secs = ready.reduce((a, i) => a + (i.duration || 0), 0);
  const freshSecs = fresh.reduce((a, i) => a + (i.duration || 0), 0);
  return (
    <header className="dateline">
      <h1 className="dl-date"><span className="md">{d.md}</span><span className="yw">{d.y} · {d.w}</span></h1>
      {ready.length > 0 && (
        <div className="dl-figs">
          <Kpi label="本刊" value={big(ready.length)} unit="期" sub={fresh.length ? `近 7 天 +${fresh.length}` : "近 7 天无新刊"} />
          <Kpi label="知识单元" value={big(units)} sub={`必学 ${big(must)}`} />
          <Kpi label="视频" value={hrs(secs)} unit="小时" sub={freshSecs ? `近 7 天 +${hrs(freshSecs)}` : undefined} />
        </div>
      )}
    </header>
  );
}

/** What needs the reader: system alerts (with what fixes them), videos whose production failed, issues that did not
 *  reach Notion, decisions. */
function Attention({ data, issues, reload }: { data: any; issues: any[]; reload: () => void }) {
  const failedRun = (i: any) => !i.tuning && i.production === "FAILED" && (i.units == null || i.origin === "lock") && i.latest_run;
  const gone = issues.filter((i) => failedRun(i) && UNAVAILABLE[i.error_class]);
  const broke = issues.filter((i) => failedRun(i) && !UNAVAILABLE[i.error_class]);
  const failed = issues.filter((i) => !i.tuning && i.delivery === "FAILED");
  const mapping = issues.filter((i) => !i.tuning && i.delivery === "BLOCKED" && (i.delivery_reason || "").startsWith("NOTION_MAPPING"));
  const held = (why: string) => issues.filter((i) => !i.tuning && i.delivery === "HELD" && i.delivery_reason === why);
  const partial = held("PARTIAL");  // a08: missing what a stage did not finish; written once a retry completes it
  const maybe = held("MAYBE_HAVE").filter((i) => i.maybe);  // a05: a page written by hand it may be
  const yours = held("YOURS_ON_PAGE");  // a07: someone did something on the page it would replace
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (id: string, path: string, body?: unknown) => {
    setBusy(id);
    try { await post(path, body); } finally { setBusy(null); reload(); }
  };
  const retry = (sid: string) => act(sid, `/api/sources/${encodeURIComponent(sid)}/deliver`);
  if (!data.alerts.length && !broke.length && !gone.length && !failed.length && !partial.length && !maybe.length && !yours.length
    && !mapping.length && !data.decisions.length) return null;
  return (
    <section className="attn" aria-label="需要你">
      {mergeAlerts(data.alerts).map((a: any) => {
        const act: [string, () => void] | null = agyAlert(a) ? ["登录 agy", openAgyLogin] : null;
        const login = a.key === "cookiecloud" || a.key === "YOUTUBE_AUTH";
        return (
          <div className={`attn-row${a.severity === "info" ? "" : " bad"}`} key={a.key} role={a.severity === "info" ? "status" : "alert"}>
            <span className="mark" aria-hidden />
            <div className="attn-t">
              <h4>{a.title}</h4>
              {!act && <p>{(a.body || "").split(/`([^`]+)`/).map((part: string, i: number) => (i % 2 ? <code key={i}>{part}</code> : typeset(part)))}</p>}
            </div>
            {act ? <button className="btn zhu" onClick={act[1]}>{act[0]}</button>
              : <a className="btn sm" {...linkProps(login ? "/engine#cc" : "/engine")}>{login ? "配置与测试" : "去机房"}</a>}
          </div>
        );
      })}
      {broke.length > 0 && (
        <div className="attn-row bad">
          <span className="mark" aria-hidden />
          <div className="attn-t">
            <h4>{broke.length} 期生产失败</h4>
            <ul className="attn-list">
              {broke.slice(0, 3).map((i) => (
                <li key={i.id}><a {...linkProps(`/issue/${encodeURIComponent(i.id)}`)}><IssueTitle title={headOf(i)} /></a>
                  {ERRORS[i.error_class] && <span className="muted">{ERRORS[i.error_class]}</span>}
                  <button className="btn sm" disabled={busy === i.id}
                    onClick={() => act(i.id, `/api/runs/${encodeURIComponent(i.latest_run)}/retry`)}>重试</button></li>
              ))}
            </ul>
          </div>
          {broke.length > 3 && <a className="btn sm" {...linkProps("/engine")}>全部 {broke.length} 期</a>}
        </div>
      )}
      {gone.length > 0 && (
        <div className="attn-row">
          <span className="mark" aria-hidden />
          <div className="attn-t">
            <h4>{gone.length} 期视频不可用</h4>
            <ul className="attn-list">
              {gone.slice(0, 3).map((i) => (
                <li key={i.id}><a {...linkProps(`/issue/${encodeURIComponent(i.id)}`)}><IssueTitle title={headOf(i)} /></a>
                  <span className="muted">{UNAVAILABLE[i.error_class]}</span>
                  <button className="btn sm" disabled={busy === i.id}
                    onClick={() => act(i.id, `/api/runs/${encodeURIComponent(i.latest_run)}/ignore`)}>忽略</button></li>
              ))}
            </ul>
          </div>
          {gone.length > 3 && <a className="btn sm" {...linkProps("/engine")}>全部 {gone.length} 期</a>}
        </div>
      )}
      {failed.length > 0 && (
        <div className="attn-row bad">
          <span className="mark" aria-hidden />
          <div className="attn-t">
            <h4>{failed.length} 期没写进 Notion</h4>
            <ul className="attn-list">
              {failed.slice(0, 3).map((i) => (
                <li key={i.id}><a {...linkProps(`/issue/${encodeURIComponent(i.id)}`)}><IssueTitle title={headOf(i)} /></a>
                  <button className="btn sm" disabled={busy === i.id} onClick={() => retry(i.id)}>重试</button></li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {partial.length > 0 && (
        <div className="attn-row bad">
          <span className="mark" aria-hidden />
          <div className="attn-t">
            <h4>{partial.length} 期部分完成，补齐后写进 Notion</h4>
            <ul className="attn-list">
              {partial.slice(0, 3).map((i) => (
                <li key={i.id}><a {...linkProps(`/issue/${encodeURIComponent(i.id)}`)}><IssueTitle title={headOf(i)} /></a>
                  {i.production === "COMPLETE_PARTIAL" && (
                    <button className="btn sm" disabled={busy === i.id}
                      onClick={() => act(i.id, `/api/runs/${encodeURIComponent(i.latest_run)}/retry`)}>重试</button>
                  )}</li>
              ))}
            </ul>
          </div>
          {partial.length > 3 && <a className="btn sm" {...linkProps("/engine")}>全部 {partial.length} 期</a>}
        </div>
      )}
      {maybe.length > 0 && (
        <div className="attn-row">
          <span className="mark" aria-hidden />
          <div className="attn-t">
            <h4>{maybe.length} 期库里可能已有</h4>
            <ul className="attn-list">
              {maybe.map((i) => (
                <li key={i.id}><a {...linkProps(`/issue/${encodeURIComponent(i.id)}`)}><IssueTitle title={headOf(i)} /></a>
                  <a className="muted" href={i.maybe.url} target="_blank" rel="noreferrer">{i.maybe.title || "库里那页"} ↗</a>
                  <span className="attn-acts">
                    <button className="btn sm" disabled={busy === i.id}
                      onClick={() => act(i.id, `/api/sources/${encodeURIComponent(i.id)}/maybe`, { write: false })}>算已有</button>
                    <button className="btn sm" disabled={busy === i.id}
                      onClick={() => act(i.id, `/api/sources/${encodeURIComponent(i.id)}/maybe`, { write: true })}>照写</button>
                  </span></li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {yours.length > 0 && (
        <div className="attn-row">
          <span className="mark" aria-hidden />
          <div className="attn-t">
            <h4>{yours.length} 期旧页上有你的改动</h4>
            <ul className="attn-list">
              {yours.map((i) => (
                <li key={i.id}><a {...linkProps(`/issue/${encodeURIComponent(i.id)}`)}><IssueTitle title={headOf(i)} /></a>
                  {i.notion_url
                    ? <a className="muted" href={i.notion_url} target="_blank" rel="noreferrer">{(i.yours || []).join("、")} ↗</a>
                    : <span className="muted">{(i.yours || []).join("、")}</span>}
                  <span className="attn-acts">
                    <button className="btn sm" disabled={busy === i.id}
                      onClick={() => act(i.id, `/api/sources/${encodeURIComponent(i.id)}/replace`, { replace: false })}>留着旧页</button>
                    <button className="btn sm" disabled={busy === i.id}
                      onClick={() => act(i.id, `/api/sources/${encodeURIComponent(i.id)}/replace`, { replace: true })}>换新页</button>
                  </span></li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {mapping.length > 0 && (
        <div className="attn-row bad">
          <span className="mark" aria-hidden />
          <div className="attn-t"><h4>{mapping.length} 期等待交付：Notion 属性对不上</h4></div>
          {/* the Notion property card and a new draft live on the 调试台, not the 看板 a locked channel opens on */}
          <a className="btn sm" {...linkProps(mapping[0].channel_id ? `/channels/${encodeURIComponent(mapping[0].channel_id)}/studio` : "/channels")}>去频道修</a>
        </div>
      )}
      {data.decisions.map((x: any) => (
        <div className="attn-row" key={x.id}>
          <span className="mark" aria-hidden />
          <div className="attn-t">
            <h4><Words>{x.title}</Words></h4>
            <p>{typeset(x.body.replace(/Run-\d+\s*/g, ""))}</p>
          </div>
          {x.kind === "CONFIRM_PLAYBOOK" && (
            <a className="btn" {...linkProps(x.channel_id ? `/channels/${encodeURIComponent(x.channel_id)}/studio` : "/channels")}>查看并定稿</a>
          )}
        </div>
      ))}
    </section>
  );
}

/** Every run in production: the one furthest along drawn in full, the others a line each; then what waits. While the
 *  queue is paused nothing will be done by any time; a pause for agy's sign-in is lifted by signing in (above). */
function Presses({ data, reload }: { data: any; reload: () => void }) {
  const [open, setOpen] = useState(false);
  const [first, ...more] = data.running as any[];
  const queue: any[] = data.queue;
  if (!first && !queue.length) return null;
  const waiting = queue.filter((q) => q.production === "WAITING").length;
  const paused = data.paused;
  const parts = [first && `${data.running.length} 期在印`, queue.length > waiting && `${queue.length - waiting} 期排队`,
    waiting && `${waiting} 期等待`, paused ? "已暂停" : data.drain_seconds > 0 && `${about(data.drain_seconds)}印完`].filter(Boolean);
  const resume = paused && !(paused.reason === "AGY_AUTH_REQUIRED" && data.alerts.some(agyAlert));
  const row = (q: any) => (
    <li key={q.id}>
      <b><IssueTitle title={q.title || q.source_id} /></b>
      <span className={q.production === "WAITING" && STOPS.includes(q.wait_reason) ? "zhu" : ""}>
        {q.production === "WAITING" ? WAIT[q.wait_reason] || q.wait_reason : PRODUCTION[q.production] || q.production}
      </span>
    </li>
  );
  return (
    <section className="presses" aria-label="付印">
      <div className="rule-title"><h2>付印</h2><span className="aside">{parts.join(" · ")}</span>
        {resume && <button className="btn sm" onClick={async () => { await post("/api/queue/resume"); reload(); }}>恢复队列</button>}
      </div>
      {first && <PressLine run={first} />}
      {more.length > 0 && (
        <ol className="minipress">
          {more.map((r) => {
            const st = r.stages.filter((x: any) => x.name !== "CLEANUP");
            const done = st.filter((x: any) => ["DONE", "WARN", "SKIPPED"].includes(x.status)).length;
            const cur = st.find((x: any) => x.status === "RUNNING");
            return (
              <li key={r.id}>
                <a {...linkProps(`/issue/${encodeURIComponent(r.source_id)}`)}><IssueTitle title={r.source?.title || r.source_id} /></a>
                <span className="mp-bar" aria-hidden><i style={{ width: `${(done / Math.max(1, st.length)) * 100}%` }} /></span>
                <span className="mp-at">{cur?.label || "—"}{r.eta_seconds ? ` · ${about(r.eta_seconds)}` : ""}</span>
              </li>
            );
          })}
        </ol>
      )}
      {queue.length > 0 && (
        <div className="waitq">
          <ol>{queue.slice(0, 3).map(row)}</ol>
          {queue.length > 3 && (
            <>
              <Fold open={open}><ol>{queue.slice(3).map(row)}</ol></Fold>
              <button className="btn ghost sm" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "收起" : `另外 ${queue.length - 3} 期`}</button>
            </>
          )}
        </div>
      )}
    </section>
  );
}

/** 出刊: the issues made each day, as a calendar. */
function Calendar({ ready }: { ready: any[] }) {
  const wide = useMedia("(min-width: 901px)");
  const weeks = wide ? 26 : 16;
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const i of ready) {
      const t = at(i.ready_at);
      if (!Number.isNaN(t)) { const k = ymd(new Date(t)); c[k] = (c[k] || 0) + 1; }
    }
    return c;
  }, [ready]);
  const since = Date.now() - weeks * WEEK_MS;
  const inRange = ready.filter((i) => at(i.ready_at) >= since).length;
  const recent = ready.filter((i) => Date.now() - at(i.ready_at) < 30 * 86400 * 1000);
  const month = recent.length;
  const byChannel = Object.entries(recent.reduce((m: Record<string, number>, i) => {
    const k = i.channel || "未识别频道";
    m[k] = (m[k] || 0) + 1;
    return m;
  }, {})).sort((a, b) => b[1] - a[1]);
  // the weekly average over the weeks the journal has been printing, not over empty ones before it began
  const first = Math.min(...ready.map((i) => at(i.ready_at)).filter((t) => !Number.isNaN(t)));
  const span = Math.max(1, Math.min(weeks, Math.ceil((Date.now() - first) / WEEK_MS)));
  const most = Math.max(0, ...Object.values(counts));
  // the longest run of days in a row with an issue, ending anywhere in the window
  let streak = 0, run = 0;
  for (let d = new Date(since); d.getTime() <= Date.now(); d.setDate(d.getDate() + 1)) {
    run = counts[ymd(d)] ? run + 1 : 0;
    streak = Math.max(streak, run);
  }
  if (!inRange) return null;
  return (
    <section className="calendar" aria-label="出刊">
      <div className="rule-title"><h2>出刊</h2></div>
      <div className="cal-body">
        <Heat counts={counts} weeks={weeks} label="每天成刊的期数"
          tip={(d, n) => <><b>{n ? `${n} 期` : "无"}</b> {d.getMonth() + 1}月{d.getDate()}日 周{"日一二三四五六"[d.getDay()]}</>} />
        <div className="cal-side">
          <div className="cal-figs">
            <Kpi label="近 30 天" value={month} unit="期" />
            <Kpi label="每周" value={(ready.filter((i) => at(i.ready_at) >= Date.now() - span * WEEK_MS).length / span).toFixed(1)} unit="期"
              sub={`近 ${span} 周平均`} />
            <Kpi label="最长连续" value={streak} unit="天" />
            <Kpi label="最多一天" value={most} unit="期" />
          </div>
          {byChannel.length > 1 && (
            <div className="cal-ch">
              <h4>近 30 天，各频道</h4>
              <HBars label="近 30 天各频道成刊的期数"
                rows={byChannel.slice(0, 5).map(([k, n]) => ({ key: k, label: k, value: n, text: `${n} 期` }))} />
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

export default function Issues() {
  const { data, error, loadedAt, reload } = usePoll<any>("/api/overview", 8000);
  useRestoreScroll(!!data);
  useEffect(() => {
    const on = () => reload();
    addEventListener("az:refresh", on);
    return () => removeEventListener("az:refresh", on);
  }, [reload]);
  if (!data) return <main className="page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const issues: any[] = mockIssues(data.issues);
  // a sample (tried out while its channel is tuned, not in Notion) is not an issue: not counted, never 本期
  const ready = issues.filter((i) => i.units != null && !i.sample);
  // newest by the video's own date: an older video taken in late (补录) neither becomes 本期 nor opens 往期
  const byDate = [...issues].sort((a, b) => when(b) - when(a));
  const feature = byDate.find((i) => i.units != null && !i.sample) || byDate.find((i) => !producing(i) && !i.sample && !dropped(i));
  const rest = byDate.filter((i) => i !== feature && !producing(i) && !dropped(i));  // what is still being made is under 付印

  return (
    <main className="page home">
      <Stale error={error} loadedAt={loadedAt} />
      <Dateline ready={ready} />
      <Attention data={data} issues={issues} reload={reload} />
      <Presses data={data} reload={reload} />
      {feature ? (
        <>
          <div className="rule-title"><h2>本期</h2></div>
          <Feature it={feature} />
          <Calendar ready={ready} />
          {rest.length > 0 && <Archive issues={rest} />}
        </>
      ) : !data.running.length && (
        <Empty title="还没有任何一期" action={<button className="btn solid" onClick={() => dispatchEvent(new Event("az:paste"))}>付印第一期</button>} />
      )}
    </main>
  );
}
