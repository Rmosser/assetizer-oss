import { post } from "../api";
import { usePoll } from "../hooks";
import { linkProps } from "../router";
import { ago, bytes, PRODUCTION, WAIT } from "../util";

function Lamp({ state, label }: { state: "ok" | "bad" | "idle"; label: string }) {
  return <span className="lbl"><i className={`lamp ${state}`} aria-hidden /><span className="caps" style={{ color: "var(--ink-2)" }}>{label}</span></span>;
}

const fmtK = (n?: number | null) => (n == null ? "—" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n));

export default function Engine() {
  const { data, reload } = usePoll<any>("/api/engine", 10000);
  if (!data) return <div className="loading">展卷</div>;
  const d = data.disk;
  const used = d.total - d.free;
  const pct = (x: number) => `${Math.max(0.4, (x / d.total) * 100)}%`;
  const other = Math.max(0, used - d.media - d.runs - d.packages);
  const agy = data.agy;
  const yt = data.alerts.find((a: any) => a.key === "YOUTUBE_AUTH" && !a.resolved_at);
  const c = data.calls || {};
  return (
    <main className="page">
      <section className="hero" style={{ gridTemplateColumns: "1fr auto" }}>
        <h1 className="hero-date" style={{ fontSize: "clamp(56px, 7vw, 104px)" }}>机房<small>付印设备与生产记录</small></h1>
        <div style={{ textAlign: "right" }}>
          {data.paused ? (
            <>
              <div className="zhupi-link" style={{ marginTop: 0 }}>队列已暂停 · {WAIT[data.paused.reason] || data.paused.reason}</div>
              <div style={{ marginTop: 12 }}><button className="btn zhu sm" onClick={async () => { await post("/api/queue/resume"); reload(); }}>手动恢复</button></div>
            </>
          ) : <div className="zhupi-quiet" style={{ marginTop: 0 }}>队列运行正常</div>}
        </div>
      </section>

      <div className="instruments">
        <div className="inst">
          <Lamp state={agy ? (agy.ok ? "ok" : "bad") : "idle"} label="模型 · agy" />
          <div className="big">{agy ? (agy.ok ? "在线" : "需要登录") : "未探测"}</div>
          <div className="sub">{data.model}{agy?.checked_at ? ` · ${ago(agy.checked_at)}检查` : " · 每次付印前自动探测"}</div>
        </div>
        <div className="inst">
          <Lamp state={yt ? "bad" : data.youtube.profile_ok ? "ok" : "idle"} label="YouTube 会话" />
          <div className="big">{yt ? "需要登录" : data.youtube.profile_ok ? "已就绪" : "未配置"}</div>
          <div className="sub">远程屏幕登录：<code style={{ fontFamily: "var(--mono)", fontSize: 11.5 }}>ssh -L 6080:127.0.0.1:6080 ovh-investment</code></div>
        </div>
        <div className="inst">
          <Lamp state={data.notion.token && (data.notion.database || data.notion.parent) ? "ok" : "idle"} label="Notion 交付" />
          <div className="big">{data.notion.token ? (data.notion.database ? "已连接" : "待指定位置") : "未配置"}</div>
          <div className="sub">{data.notion.token ? "数据库每期一行，正文含视频与截图" : "配置 token 前，可以在应用内阅读与审阅"}</div>
        </div>
        <div className="inst">
          <Lamp state={d.free / d.total < 0.15 ? "bad" : "ok"} label="磁盘" />
          <div className="big">{bytes(d.free)} <span style={{ font: "500 14px var(--sans)", color: "var(--ink-3)" }}>可用</span></div>
          <div className="diskbar" aria-label="磁盘占用">
            <i className="media" style={{ width: pct(d.media) }} title="原始媒体" />
            <i style={{ width: pct(d.runs) }} title="生产工作区" />
            <i className="pk" style={{ width: pct(d.packages) }} title="发布素材包" />
            <i style={{ width: pct(other), background: "var(--rule-2)" }} title="其他" />
          </div>
          <div className="legend">
            <span style={{ color: "var(--ink-2)" }}>媒体 {bytes(d.media)}</span>
            <span style={{ color: "var(--ink)" }}>工作区 {bytes(d.runs)}</span>
            <span style={{ color: "var(--ink-3)" }}>素材包 {bytes(d.packages)}</span>
          </div>
        </div>
      </div>
      <div className="tiles">
        <div className="tile"><span className="caps">模型调用</span><div className="v">{c.n ?? 0}<small>次</small></div></div>
        <div className="tile"><span className="caps">模型耗时</span><div className="v">{c.wall ? Math.round(c.wall / 60) : 0}<small>分钟</small></div></div>
        <div className="tile"><span className="caps">Token（入 / 出 / 思考）</span><div className="v" style={{ fontSize: 26 }}>{fmtK(c.inp)} / {fmtK(c.outp)} / {fmtK(c.think)}</div></div>
        <div className="tile"><span className="caps">agy 峰值内存</span><div className="v">{c.rss ? (c.rss / 1048576).toFixed(1) : "—"}<small>GiB</small></div></div>
      </div>
      <p className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>{data.capacity_note} 金额成本：provider 未暴露，暂不统计。</p>

      <div className="cols2">
        <div>
          <div className="rule-title"><h2>生产记录</h2></div>
          <table className="runs">
            <thead><tr><th>期</th><th>阶段</th><th>状态</th><th></th></tr></thead>
            <tbody>
              {data.runs.map((r: any) => (
                <tr key={r.id}>
                  <td className="t"><a {...linkProps(`/issue/${encodeURIComponent(r.source_id)}`)}>{r.title || r.source_id}</a>
                    <div className="mono muted" style={{ fontSize: 11.5 }}>{r.id} · {ago(r.created_at)}</div></td>
                  <td><div className="minirail" aria-label="阶段">{r.stages.map((s: any) => <i key={s.name} className={s.status} title={`${s.label} · ${s.status}`} />)}</div></td>
                  <td><span className={`state-tag ${["FAILED", "WAITING"].includes(r.production) ? "bad" : r.production?.startsWith("COMPLETE") ? "ok" : ""}`}>
                    {PRODUCTION[r.production] || r.production}{r.wait_reason ? ` · ${WAIT[r.wait_reason] || r.wait_reason}` : ""}{r.error_class ? ` · ${r.error_class}` : ""}</span></td>
                  <td>{["FAILED", "WAITING", "COMPLETE_PARTIAL"].includes(r.production) && r.kind !== "IMPORT" &&
                    <button className="btn sm" onClick={async () => { await post(`/api/runs/${r.id}/retry`); reload(); }}>重试</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <div className="rule-title"><h2>日志</h2></div>
          <div className="log" role="log">
            {data.events.map((e: any) => (
              <div key={e.id} className={`lv-${e.level}`}>
                <span className="muted">{e.ts.replace("T", " ").slice(5, 19)}</span>
                <span>{e.kind}</span>
                <span>{e.message}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </main>
  );
}
