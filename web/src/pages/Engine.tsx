import { useState } from "react";
import { post } from "../api";
import { deJargon, issueHeadline } from "../cjk";
import { Fault, Stale, Unroll } from "../components/States";
import { useMedia, usePoll } from "../hooks";
import { linkProps, useRestoreScroll } from "../router";
import { ago, bytes, clock, PRODUCTION, WAIT } from "../util";

function Lamp({ state, label }: { state: "ok" | "bad" | "idle"; label: string }) {
  return <span className="lbl"><i className={`lamp ${state}`} aria-hidden /><span className="label">{label}</span></span>;
}

const fmtK = (n?: number | null) => (n == null ? "—" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n));
const SSH = "ssh -N -L 6080:127.0.0.1:6080 ovh-investment";
const KIND: Record<string, string> = { worker: "机房", stage: "工序", run: "付印", delivery: "交付", review: "审阅", compile: "编译", ingest: "收录", alert: "告警", experiment: "实验" };
/** "CLEANUP 完成" → "清理 完成": stage codes in log lines read as their Chinese labels */
const humanize = (msg: string, labels: Record<string, string>) =>
  deJargon(msg || "").replace(/\b[A-Z][A-Z_]{2,}\b/g, (m) => labels[m] || m).replace(/^Worker /, "机房");
/** consecutive identical events collapse into one line with a count */
const collapse = (events: any[]) => events.reduce((a: any[], e: any) => {
  const l = a[a.length - 1];
  if (l && l.kind === e.kind && l.message === e.message) { l.n++; return a; }
  a.push({ ...e, n: 1 });
  return a;
}, []);

function Cmd({ text }: { text: string }) {
  const [ok, setOk] = useState(false);
  return (
    <span className="cmd">
      <code>{text}</code>
      <button onClick={async () => { try { await navigator.clipboard.writeText(text); setOk(true); setTimeout(() => setOk(false), 1500); } catch { /* ignore */ } }}>
        {ok ? "已复制" : "复制"}
      </button>
    </span>
  );
}

function StageBar({ stages }: { stages: any[] }) {
  return (
    <span className="stagebar" aria-label="阶段">
      {stages.filter((s) => s.name !== "CLEANUP").map((s) => <i key={s.name} className={s.status} title={`${s.label} · ${s.status}`} />)}
    </span>
  );
}

function RunRow({ r, reload }: { r: any; reload: () => void }) {
  const bad = ["FAILED", "WAITING"].includes(r.production);
  return (
    <li className="run">
      <span className="run-id num">{r.id.replace(/^.*?-(r\d+)$/, "$1")}</span>
      <span className="run-when" title={r.created_at}>{ago(r.created_at)}</span>
      <StageBar stages={r.stages} />
      <span className={`run-state${bad ? " bad" : ""}`}>
        {PRODUCTION[r.production] || r.production}{r.wait_reason ? ` · ${WAIT[r.wait_reason] || r.wait_reason}` : ""}{r.error_class ? ` · ${r.error_class}` : ""}
      </span>
      {["FAILED", "WAITING", "COMPLETE_PARTIAL"].includes(r.production) && r.kind !== "IMPORT" &&
        <button className="btn sm" onClick={async () => { await post(`/api/runs/${r.id}/retry`); reload(); }}>重试</button>}
    </li>
  );
}

function Runs({ runs, reload }: { runs: any[]; reload: () => void }) {
  const groups: { sid: string; title: string; prod: any[]; exp: any[] }[] = [];
  for (const r of runs) {
    let g = groups.find((x) => x.sid === r.source_id);
    if (!g) groups.push((g = { sid: r.source_id, title: issueHeadline(r.headline, null, r.title || r.source_id), prod: [], exp: [] }));
    (r.kind === "EXPERIMENT" ? g.exp : g.prod).push(r);
  }
  return (
    <div className="runs">
      {groups.map((g) => (
        <section key={g.sid} className="run-group">
          <a className="run-title" {...linkProps(`/issue/${encodeURIComponent(g.sid)}`)}>{g.title}</a>
          <ol>{g.prod.map((r) => <RunRow key={r.id} r={r} reload={reload} />)}</ol>
          {g.exp.length > 0 && (
            <details className="fold mini">
              <summary><i className="plus" aria-hidden /><span>{`实验运行 ${g.exp.length} 次`}</span></summary>
              <ol>{g.exp.map((r) => <RunRow key={r.id} r={{ ...r, id: r.id }} reload={reload} />)}</ol>
            </details>
          )}
        </section>
      ))}
    </div>
  );
}

export default function Engine() {
  const { data, error, loadedAt, reload } = usePoll<any>("/api/engine", 10000);
  const phone = useMedia("(max-width: 900px)");
  useRestoreScroll(!!data);
  if (!data) return <main className="page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const d = data.disk;
  const used = d.total - d.free;
  const pct = (x: number) => `${Math.max(0.4, (x / d.total) * 100)}%`;
  const other = Math.max(0, used - d.media - d.runs - d.packages);
  const agy = data.agy;
  const yt = data.alerts.find((a: any) => a.key === "YOUTUBE_AUTH" && !a.resolved_at);
  const c = data.calls || {};
  return (
    <main className="page">
      <Stale error={error} loadedAt={loadedAt} />
      <section className="hero page-hero">
        <h1 className="hero-title">机房</h1>
        <div className="hero-lede">
          付印设备与生产记录。{data.capacity_note.replace(/^串行处理：/, "串行付印：")}
          <div className={`queue-state${data.paused ? " bad" : ""}`}>
            {data.paused ? (
              <>队列已暂停 · {WAIT[data.paused.reason] || data.paused.reason}
                <button className="btn zhu sm" onClick={async () => { await post("/api/queue/resume"); reload(); }}>手动恢复</button></>
            ) : <>队列运行正常</>}
          </div>
        </div>
      </section>

      <div className="instruments">
        <div className="inst">
          <Lamp state={agy ? (agy.ok ? "ok" : "bad") : "idle"} label="模型" />
          <div className="big">{agy ? (agy.ok ? "在线" : "需要登录") : "未探测"}</div>
          <div className="sub"><span className="num">{data.model}</span>{agy?.checked_at ? ` · ${ago(agy.checked_at)}检查` : " · 每次付印前自动探测"}</div>
        </div>
        <div className="inst">
          <Lamp state={yt ? "bad" : data.youtube.profile_ok ? "ok" : "idle"} label="YouTube 会话" />
          <div className="big">{yt ? "需要登录" : data.youtube.profile_ok ? "已就绪" : "未配置"}</div>
          {phone ? <div className="sub">需要重新登录时，在电脑上打开机房操作。</div> : (
            <>
              <div className="sub">远程屏幕登录需要先在电脑上建隧道：</div>
              <Cmd text={SSH} />
              <button className="btn sm inst-act" onClick={async () => { const r = await post("/api/youtube/remote-browser"); if (r.ok) window.open(r.url, "_blank"); else alert(r.output); }}>启动远程登录屏幕</button>
            </>
          )}
        </div>
        <div className="inst">
          <Lamp state={data.notion.token && (data.notion.database || data.notion.parent) ? "ok" : "idle"} label="Notion 交付" />
          <div className="big">{data.notion.token ? (data.notion.database ? "已连接" : "待指定位置") : "未配置"}</div>
          <div className="sub">{data.notion.token ? "数据库每期一行，正文含视频与截图" : "配置之前，可以在应用内阅读与审阅"}</div>
        </div>
        <div className="inst">
          <Lamp state={d.free / d.total < 0.15 ? "bad" : "ok"} label="磁盘" />
          <div className="big"><span className="num">{bytes(d.free)}</span><small>可用</small></div>
          <div className="diskbar" role="img" aria-label={`媒体 ${bytes(d.media)}，工作区 ${bytes(d.runs)}，素材包 ${bytes(d.packages)}`}>
            <i className="media" style={{ width: pct(d.media) }} />
            <i className="work" style={{ width: pct(d.runs) }} />
            <i className="pk" style={{ width: pct(d.packages) }} />
            <i className="other" style={{ width: pct(other) }} />
          </div>
          <div className="legend">
            <span className="media">媒体 {bytes(d.media)}</span>
            <span className="work">工作区 {bytes(d.runs)}</span>
            <span className="pk">素材包 {bytes(d.packages)}</span>
          </div>
        </div>
        <div className="inst tile"><span className="label">模型调用</span><div className="v"><span className="num">{c.n ?? 0}</span><small>次</small></div></div>
        <div className="inst tile"><span className="label">模型耗时</span><div className="v"><span className="num">{c.wall ? Math.round(c.wall / 60) : 0}</span><small>分钟</small></div></div>
        <div className="inst tile"><span className="label">Token · 入 / 出 / 思考</span><div className="v sm"><span className="num">{fmtK(c.inp)} / {fmtK(c.outp)} / {fmtK(c.think)}</span></div></div>
        <div className="inst tile"><span className="label">模型峰值内存</span><div className="v"><span className="num">{c.rss ? (c.rss / 1048576).toFixed(1) : "—"}</span><small>GiB</small></div></div>
      </div>
      <p className="colophon">金额成本：模型服务商没有提供，暂不统计。</p>

      <div className="cols2">
        <div>
          <div className="rule-title"><h2>生产记录</h2></div>
          <Runs runs={data.runs} reload={reload} />
        </div>
        <div>
          <div className="rule-title"><h2>日志</h2></div>
          <ol className="log" role="log">
            {collapse(data.events).map((e: any) => (
              <li key={e.id} className={`lv-${e.level}`}>
                <span className="when num" title={e.ts}>{clock(e.ts)}</span>
                <span className="kind">{KIND[e.kind] || e.kind}</span>
                <span className="msg">{humanize(e.message, data.stage_labels || {})}{e.n > 1 && <span className="muted"> ×{e.n}</span>}</span>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </main>
  );
}
