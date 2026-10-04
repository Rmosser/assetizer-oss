import { useEffect } from "react";
import { media, post } from "../api";
import { usePoll } from "../hooks";
import { linkProps } from "../router";
import { cnDate, dur, pubDate, tc, WAIT } from "../util";

type Stage = { name: string; label: string; status: string; started_at?: string };

export function PressLine({ run }: { run: any }) {
  const stages: Stage[] = run.stages.filter((s: Stage) => s.name !== "CLEANUP");
  const idx = Math.max(0, stages.findIndex((s) => s.status === "RUNNING" || s.status === "PENDING"));
  const doneCount = stages.filter((s) => ["DONE", "WARN", "SKIPPED"].includes(s.status)).length;
  const pct = Math.min(100, (doneCount / (stages.length - 1)) * 100);
  const eta = run.eta_seconds ? Math.max(1, Math.round(run.eta_seconds / 60)) : null;
  const current = stages.find((s) => s.status === "RUNNING") || stages[idx];
  return (
    <section className="press" aria-label="正在付印">
      <div className="press-head">
        <span className="caps zhu">正在付印</span>
        <h3>{run.source?.title || run.source_id}</h3>
        <span className="eta">{current?.label}{eta ? ` · 约 ${eta} 分钟后可读` : ""}</span>
      </div>
      <div className="rail" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
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

function Toc({ issues }: { issues: any[] }) {
  return (
    <ol className="toc stagger">
      {issues.map((it) => {
        const producing = ["QUEUED", "RUNNING", "WAITING", "INTERRUPTED"].includes(it.production) && !it.units;
        return (
          <li key={it.id}>
            <a className="toc-row" {...linkProps(`/issue/${encodeURIComponent(it.id)}`)}>
              <div className="toc-no">No.<b>{String(it.no).padStart(3, "0")}</b></div>
              <div className="cover">
                {it.cover ? <img src={media(it.cover)} alt="" loading="lazy" /> : <span className="ph">刊</span>}
              </div>
              <div className="toc-title">
                <div className="chan">{it.channel || "未识别频道"}</div>
                <h3>{it.title}</h3>
                <div className="meta">{pubDate(it.published_at)} · {tc(it.duration)}</div>
              </div>
              <div className="toc-stats">
                {it.units != null ? (
                  <>
                    <div><b>{it.units}</b>个知识单元</div>
                    <div><b>{it.must}</b>个必学 · <b>{it.clips}</b>段片段</div>
                  </>
                ) : <div className="muted">尚未成刊</div>}
              </div>
              <div className="toc-state">
                {producing ? (
                  <span className="wip">{it.production === "WAITING" ? "等待中" : it.production === "RUNNING" ? "付印中" : "排队中"}</span>
                ) : it.flags > 0 ? (
                  <span className="pi">朱批 {it.flags}</span>
                ) : it.review === "APPROVED" ? (
                  <span className="ok">已审</span>
                ) : (
                  <span className="muted">待阅</span>
                )}
                <small>{it.delivery === "DELIVERED" ? "已入 Notion" : it.delivery === "BLOCKED" ? "Notion 未配置" : ""}</small>
              </div>
            </a>
          </li>
        );
      })}
    </ol>
  );
}

export default function Issues() {
  const { data, reload } = usePoll<any>("/api/overview", 8000);
  useEffect(() => {
    const on = () => reload();
    addEventListener("az:refresh", on);
    return () => removeEventListener("az:refresh", on);
  }, [reload]);
  const d = cnDate();
  if (!data) return <div className="loading">展卷</div>;
  const issues: any[] = data.issues;
  const ready = issues.filter((i) => i.units != null);
  const totalUnits = ready.reduce((a, i) => a + (i.units || 0), 0);
  const totalClips = ready.reduce((a, i) => a + (i.clips || 0), 0);
  const firstFlagged = issues.find((i) => i.flags > 0);

  return (
    <main className="page">
      <section className="hero">
        <h1 className="hero-date">{d.md}<small>{d.y}</small></h1>
        <div>
          <p className="hero-lede">
            本刊已收 <strong>{ready.length}</strong> 期，沉淀 <strong>{totalUnits}</strong> 个知识单元、<strong>{totalClips}</strong> 段可独立观看的视频片段。
            {data.queue.length > 0 && <> 另有 <strong>{data.queue.length}</strong> 期在排队。</>}
          </p>
          {data.open_flags > 0 && firstFlagged ? (
            <a className="zhupi-link" {...linkProps(`/issue/${encodeURIComponent(firstFlagged.id)}`)}>
              朱批 {data.open_flags} 处，待你判断 <span className="arrow">→</span>
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
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" style={{ color: "var(--zhu)", marginTop: 3 }}><path d="M12 3 2.5 20h19L12 3Z" strokeLinejoin="round" /><path d="M12 10v4.5M12 17.2v.3" strokeLinecap="round" /></svg>
              <div>
                <h4>{a.title}</h4>
                <p>{(a.body || "").split(/`([^`]+)`/).map((part: string, i: number) => (i % 2 ? <code key={i}>{part}</code> : part))}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {data.decisions.length > 0 && (
        <>
          <div className="rule-title"><h2>待你拍板</h2></div>
          {data.decisions.map((x: any) => (
            <div className="decision" key={x.id}>
              <span className="mark" />
              <div>
                <h4>{x.title}</h4>
                <p>{x.body}</p>
              </div>
              {x.kind === "CONFIRM_PLAYBOOK" ? (
                <a className="btn" {...linkProps("/channels")}>查看并确认</a>
              ) : null}
            </div>
          ))}
        </>
      )}

      {data.producing && <PressLine run={data.producing} />}
      {data.queue.length > 0 && (
        <div className="queue">
          <span className="caps">队列</span>
          {data.queue.map((q: any) => (
            <span key={q.id}><b>{q.title || q.source_id}</b>{q.production === "WAITING" ? ` · ${WAIT[q.wait_reason] || q.wait_reason}` : ""}</span>
          ))}
          {data.paused && <button className="btn sm zhu" onClick={async () => { await post("/api/queue/resume"); reload(); }}>恢复队列</button>}
        </div>
      )}

      <div className="rule-title">
        <h2>目录</h2>
        <span className="aside caps">按收录时间 · 新近在前</span>
      </div>
      {issues.length === 0 ? (
        <div className="empty">
          <h3>还没有任何一期</h3>
          <p>在右上角粘贴一个 YouTube 视频链接。系统会自动理解、拆解成知识单元，并剪好片段、写好文章——你只需要回来阅读。</p>
        </div>
      ) : (
        <Toc issues={issues} />
      )}
      <p className="muted" style={{ marginTop: 28, fontSize: 12.5 }}>
        串行付印：一小时视频约 1.5–2 小时成刊。{dur(issues.reduce((a, i) => a + (i.duration || 0), 0))} 原始内容已入库。
      </p>
    </main>
  );
}
