import { useEffect, useMemo, useState } from "react";
import { media, post } from "../api";
import { IssueTitle, splitTitle, typeset, Words } from "../cjk";
import { Yin } from "../components/Yin";
import { Empty, Fault, Stale, Unroll } from "../components/States";
import { usePoll } from "../hooks";
import { linkProps, useRestoreScroll } from "../router";
import { cnDate, cnDay, cnMonth, cnNum, dur, mins, parseDay, WAIT } from "../util";

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

const producing = (it: any) => ["QUEUED", "RUNNING", "WAITING", "INTERRUPTED"].includes(it.production) && !it.units;
const issueHref = (it: any, hash = "") => `/issue/${encodeURIComponent(it.id)}${hash}`;

function State({ it }: { it: any }) {
  if (producing(it)) return <span className="st wip">{it.production === "WAITING" ? "等待中" : it.production === "RUNNING" ? "付印中" : "排队中"}</span>;
  if (it.review === "APPROVED") return <Yin size={34} />;
  if (it.flags > 0) return <span className="st pi">批 <b className="num">{it.flags}</b></span>;
  return <span className="st">待阅</span>;
}

/** The newest issue, set like a feature page: cover, title, the three must-reads, and what needs a decision. */
function Feature({ it }: { it: any }) {
  return (
    <section className="feature" aria-label="本期">
      <a className="feature-cover" {...linkProps(issueHref(it))} tabIndex={-1} aria-hidden>
        {it.cover ? <img src={media(it.cover)} alt="" fetchPriority="high" /> : <span className="ph">刊</span>}
      </a>
      <div className="feature-text">
        <div className="kicker">{it.channel || "未识别频道"} · 第{cnNum(it.no)}期{it.published_at ? ` · ${cnDay(it.published_at)}` : ""}</div>
        <h2 className="feature-title"><a {...linkProps(issueHref(it))}><Words>{splitTitle(it.title).head}</Words></a></h2>
        {splitTitle(it.title).dek && <p className="dek"><Words>{splitTitle(it.title).dek}</Words></p>}
        {it.units != null ? (
          <p className="feature-stats"><span className="num">{it.units}</span> 单元 · 必学 <span className="num">{it.must}</span> · <span className="num">{it.clips}</span> 段片段 · <span className="num">{mins(it.duration)}</span></p>
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
          {it.review === "APPROVED" ? (
            <span className="approved"><Yin size={36} />已审定</span>
          ) : it.flags > 0 ? (
            <a className="go zhu" {...linkProps(issueHref(it))}>朱批 <span className="num">{it.flags}</span> 处待判断 <span className="arrow">→</span></a>
          ) : (
            <a className="go" {...linkProps(issueHref(it))}>开卷阅读 <span className="arrow">→</span></a>
          )}
          {it.delivery === "DELIVERED" && <span className="muted">已入 Notion</span>}
        </div>
      </div>
    </section>
  );
}

function Row({ it }: { it: any }) {
  return (
    <li>
      <a className="toc-row" {...linkProps(issueHref(it))}>
        <div className="toc-no"><span className="num">{String(it.no).padStart(3, "0")}</span></div>
        <div className="cover">
          {it.cover ? <img src={media(it.cover)} alt="" loading="lazy" /> : <span className="ph">刊</span>}
        </div>
        <div className="toc-title">
          <h3><IssueTitle title={it.title} /></h3>
          <div className="meta">{it.channel || "未识别频道"} · {cnDay(it.published_at)} · {mins(it.duration)}{it.units != null && <> · {it.units} 单元</>}</div>
        </div>
        <div className="toc-state"><State it={it} /></div>
      </a>
    </li>
  );
}

const FILTERS: [string, string, (it: any) => boolean][] = [
  ["all", "全部", () => true],
  ["flags", "有朱批", (it) => it.flags > 0],
  ["unreviewed", "未审", (it) => it.units != null && it.review !== "APPROVED"],
];

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
        {issues.length >= 4 && (
          <div className="chips" role="group" aria-label="筛选">
            {FILTERS.map(([k, label]) => <button key={k} className={f === k ? "on" : ""} onClick={() => setF(k)}>{label}</button>)}
            {channels.length > 1 && channels.map((c) => <button key={c} className={f === c ? "on" : ""} onClick={() => setF(c)}>{c}</button>)}
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
    return { ...base, id: `${base.id}`, no: n - i, flags: i % 3 ? 0 : base.flags, review: i % 4 === 1 ? "APPROVED" : base.review,
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
  if (!data) return error ? <Fault error={error} retry={reload} /> : <Unroll />;
  const d = cnDate();
  const issues: any[] = mockIssues(data.issues);
  const ready = issues.filter((i) => i.units != null);
  const totalUnits = ready.reduce((a, i) => a + (i.units || 0), 0);
  const totalClips = ready.reduce((a, i) => a + (i.clips || 0), 0);
  const firstFlagged = issues.find((i) => i.flags > 0);
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
          {data.open_flags > 0 && firstFlagged ? (
            <a className="zhupi-link" {...linkProps(issueHref(firstFlagged))}>
              <span>朱批 <span className="num">{data.open_flags}</span> 处，待你判断</span><span className="arrow" aria-hidden>→</span>
            </a>
          ) : ready.length > 0 ? (
            <div className="zhupi-quiet">无待判断事项，可以安心阅读。</div>
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
        <section className="decisions" aria-label="待你拍板">
          <div className="rule-title"><h2>待你拍板</h2></div>
          {data.decisions.map((x: any) => (
            <div className="decision" key={x.id}>
              <span className="mark" aria-hidden />
              <div className="decision-text">
                <h4><Words>{x.title}</Words></h4>
                <p>{typeset(x.body.replace(/Run-\d+\s*/g, ""))}</p>
              </div>
              {x.kind === "CONFIRM_PLAYBOOK" ? <a className="btn" {...linkProps("/channels")}>查看并确认</a> : null}
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
        <Empty title="还没有任何一期">
          点右上角的「＋」或在搜索框粘贴一个 YouTube 视频链接。系统会自动理解、拆解成知识单元，并剪好片段、写好文章——你只需要回来阅读。
        </Empty>
      )}
      {issues.length > 0 && (
        <p className="colophon">已入库原始视频共 {dur(totalMin)}。串行付印：一小时视频约 1.5–2 小时成刊。</p>
      )}
    </main>
  );
}
