import { useEffect, useMemo, useState } from "react";
import { media, post, srcSet } from "../api";
import { IssueTitle, issueHeadline, originalTitle, typeset, Words } from "../cjk";
import { Empty, Fault, Stale, Unroll } from "../components/States";
import { usePoll } from "../hooks";
import { linkProps, useRestoreScroll } from "../router";
import { cnDate, cnDay, cnMonth, cnNum, dur, parseDay, WAIT } from "../util";

type Stage = { name: string; label: string; status: string; started_at?: string };

export function PressLine({ run }: { run: any }) {
  const [reveal, setReveal] = useState(false);
  const stages: Stage[] = run.stages.filter((s: Stage) => s.name !== "CLEANUP");
  const idx = Math.max(0, stages.findIndex((s) => s.status === "RUNNING" || s.status === "PENDING"));
  const doneCount = stages.filter((s) => ["DONE", "WARN", "SKIPPED"].includes(s.status)).length;
  const pct = Math.min(100, (doneCount / (stages.length - 1)) * 100);
  const eta = run.eta_seconds ? Math.max(1, Math.round(run.eta_seconds / 60)) : null;
  const current = stages.find((s) => s.status === "RUNNING") || stages[idx];
  const at = stages.indexOf(current) + 1;
  return (
    <section className="press" aria-label="正在付印">
      <div className="press-head">
        <span className="label zhu">正在付印</span>
        <h3><IssueTitle title={run.source?.title || run.source_id} /></h3>
        <span className="eta">{current?.label}{eta ? ` · 约 ${eta} 分钟后可读` : ""}</span>
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
const issueHref = (it: any, hash = "") => `/issue/${encodeURIComponent(it.id)}${hash}`;

/** Quiet when all is well (the issue is in Notion); speaks only for samples, production and delivery trouble. */
function State({ it }: { it: any }) {
  if (producing(it)) return <span className="st wip">{it.production === "WAITING" ? "等待中" : it.production === "RUNNING" ? "付印中" : "排队中"}</span>;
  if (it.tuning) return <span className="st" title="调试中频道的样片，定稿后写入 Notion">样片</span>;
  if (it.delivery === "FAILED") return <span className="st pi">交付失败</span>;
  if (it.delivery === "REGISTERED") return <span className="st" title="Notion 里已有这个视频的页面，没有重复写入">已登记</span>;
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
        {originalTitle(it.title) !== headOf(it) && <p className="dek"><span className="dek-k">原题</span><Words>{originalTitle(it.title)}</Words></p>}
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
          ) : <span className="muted">{it.tuning ? "调试样片，定稿后写入 Notion" : "尚未写入 Notion"}</span>}
          <a className="go quiet" {...linkProps(issueHref(it))}>对照原视频 <span className="arrow">→</span></a>
        </div>
      </div>
    </section>
  );
}

const SIXTY_DAYS = 60 * 86400 * 1000;

function Row({ it }: { it: any }) {
  // issues older than sixty days collapse into a single 44px line: no cover, title, date, state
  const d = parseDay(it.published_at) || parseDay(it.updated_at);
  const compact = !!d && Date.now() - d.getTime() > SIXTY_DAYS;
  return (
    <li>
      <a className={`toc-row${compact ? " compact" : ""}`} {...linkProps(issueHref(it))}>
        <div className="toc-no"><span className="num">{String(it.no).padStart(3, "0")}</span></div>
        <div className="cover">
          {it.cover ? <img src={media(it.cover, 320)} srcSet={srcSet(it.cover, [320, 640])} sizes="120px" alt="" loading="lazy" /> : <span className="ph">刊</span>}
        </div>
        <div className="toc-title">
          <h3><Words>{headOf(it)}</Words></h3>
          <div className="meta">
            {(compact ? [cnDay(it.published_at)] : [it.channel || "未识别频道", cnDay(it.published_at), `${Math.round((it.duration || 0) / 60)} 分钟`, it.units != null && `${it.units} 单元`])
              .filter(Boolean).map((x, i) => <span key={i} className="ph">{i > 0 && " · "}{x}</span>)}
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
    return { ...base, id: `${base.id}`, no: n - i, tuning: i % 7 === 2, delivery: i % 9 === 4 ? "FAILED" : base.delivery,
      published_at: `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}` };
  });
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
  const d = cnDate();
  const issues: any[] = mockIssues(data.issues);
  const ready = issues.filter((i) => i.units != null);
  const totalUnits = ready.reduce((a, i) => a + (i.units || 0), 0);
  const totalClips = ready.reduce((a, i) => a + (i.clips || 0), 0);
  const feature = ready[0] || issues.find((i) => !producing(i));
  const rest = issues.filter((i) => i !== feature && !(producing(i) && data.producing?.source_id === i.id));
  const totalMin = issues.reduce((a, i) => a + (i.duration || 0), 0);

  return (
    <main className="page home">
      <Stale error={error} loadedAt={loadedAt} />
      <section className="hero">
        <h1 className="hero-date">
          <span className="md">{d.md}</span>
          <span className="yw" aria-label={`${d.y}${d.w}`}><span>{d.y}</span><span>{d.w}</span></span>
        </h1>
        <div className="hero-side">
          {ready.length > 0 ? (
            <p className="hero-lede">
              本刊已收 <strong className="num">{ready.length}</strong> 期，沉淀 <strong className="num">{totalUnits}</strong> 个知识单元、<strong className="num">{totalClips}</strong> 段可独立观看的片段。
              {data.queue.length > 0 && <> 另有 <strong className="num">{data.queue.length}</strong> 期在排队。</>}
            </p>
          ) : <p className="hero-lede">一份只为你编的刊：视频进来，知识单元、片段与文章出去。</p>}
          {data.decisions.length > 0 ? (
            <a className="zhupi-link" href="#decisions">
              <span>待你拍板 <span className="num">{data.decisions.length}</span> 件</span><span className="arrow" aria-hidden>→</span>
            </a>
          ) : ready.length > 0 ? (
            <div className="zhupi-quiet">没有需要你处理的事。新的一期会自动写入 Notion。</div>
          ) : null}
        </div>
      </section>

      {(data.alerts.length > 0 || data.paused) && (
        <div className="alerts" role="alert">
          {data.alerts.map((a: any) => (
            <div className="alert" key={a.key}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden><path d="M12 3 2.5 20h19L12 3Z" strokeLinejoin="round" /><path d="M12 10v4.5M12 17.2v.3" strokeLinecap="round" /></svg>
              <div>
                <h4>{a.title}</h4>
                <p>{(a.body || "").split(/`([^`]+)`/).map((part: string, i: number) => (i % 2 ? <code key={i}>{part}</code> : typeset(part)))}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {data.decisions.length > 0 && (
        <section className="decisions" id="decisions" aria-label="待你拍板">
          <div className="rule-title"><h2>待你拍板</h2></div>
          {data.decisions.map((x: any) => (
            <div className="decision" key={x.id}>
              <span className="mark" aria-hidden />
              <div className="decision-text">
                <h4><Words>{x.title}</Words></h4>
                <p>{typeset(x.body.replace(/Run-\d+\s*/g, ""))}</p>
              </div>
              {x.kind === "CONFIRM_PLAYBOOK" ? <a className="btn" {...linkProps("/channels")}>查看并定稿</a> : null}
            </div>
          ))}
        </section>
      )}

      {data.producing && <PressLine run={data.producing} />}
      {data.queue.length > 0 && (
        <div className="queue">
          <span className="label">队列</span>
          {data.queue.map((q: any) => (
            <span key={q.id}><b><IssueTitle title={q.title || q.source_id} /></b>{q.production === "WAITING" ? ` · ${WAIT[q.wait_reason] || q.wait_reason}` : ""}</span>
          ))}
          {data.paused && <button className="btn sm zhu" onClick={async () => { await post("/api/queue/resume"); reload(); }}>恢复队列</button>}
        </div>
      )}

      {feature ? (
        <>
          <div className="rule-title"><h2>本期</h2></div>
          <Feature it={feature} />
          {rest.length > 0 && <Archive issues={rest} />}
        </>
      ) : !data.producing && (
        <Empty title="还没有任何一期" action={<button className="btn solid" onClick={() => dispatchEvent(new Event("az:paste"))}>付印第一期</button>}>
          粘贴一个 YouTube 视频链接，系统会理解、拆解成知识单元，剪好片段、写好文章。你只需要回来阅读。
        </Empty>
      )}
      {issues.length > 0 && (
        <p className="colophon">已入库原始视频共 {dur(totalMin)}。串行付印：一小时视频约 <span className="ph">1.5–2 小时</span>成刊。</p>
      )}
    </main>
  );
}
