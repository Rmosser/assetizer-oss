import { useCallback, useState } from "react";
import { post } from "../api";
import { deJargon, issueHeadline } from "../cjk";
import { Cmd } from "../components/Cmd";
import { CookieCloudSheet } from "../components/CookieCloud";
import { Fault, Stale, Unroll } from "../components/States";
import { useMedia, usePoll } from "../hooks";
import { linkProps, useRestoreScroll } from "../router";
import { ago, bytes, clock, PRODUCTION, WAIT } from "../util";

function Lamp({ state, label }: { state: "ok" | "bad" | "idle"; label: string }) {
  return <span className="lbl"><i className={`lamp ${state}`} aria-hidden /><span className="label">{label}</span></span>;
}

const fmtK = (n?: number | null) => (n == null ? "—" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n));
const SSH = "ssh -N -L 6080:127.0.0.1:6080 ovh-investment";
const KIND: Record<string, string> = { worker: "机房", stage: "工序", run: "付印", delivery: "交付", review: "审阅", compile: "编译", ingest: "收录", alert: "告警", experiment: "实验", config: "配置" };
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
  const [ccOpen, setCcOpen] = useState(false);
  const closeCc = useCallback(() => setCcOpen(false), []);
  useRestoreScroll(!!data);
  if (!data) return <main className="page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const d = data.disk;
  const used = d.total - d.free;
  const pct = (x: number) => `${Math.max(0.4, (x / d.total) * 100)}%`;
  const other = Math.max(0, used - d.media - d.runs - d.packages);
  const agy = data.agy;
  const agyMedia = data.agy_media;  // can agy still read a video the allowed way (self-test after each agy update)
  const yt = data.alerts.find((a: any) => a.key === "YOUTUBE_AUTH" && !a.resolved_at);
  const cc = data.youtube.cookiecloud;
  const ccs = data.youtube.state?.source === "cookiecloud" ? data.youtube.state : null;
  const vt = data.youtube.video_test?.verdict ? data.youtube.video_test : null;
  // when CookieCloud could not be read, that is the cause to show, not the "需要登录" it leads to
  const ccDown = !!(cc?.configured && ccs && !ccs.ok);
  const [ytLamp, ytBig]: ["ok" | "bad" | "idle", string] = ccDown ? ["bad", "读不到 CookieCloud"] : yt ? ["bad", "需要登录"]
    : cc?.configured ? (ccs?.ok ? ["ok", "已就绪"] : ["idle", "待验证"]) : data.youtube.profile_ok ? ["ok", "已就绪"] : ["idle", "未配置"];
  const c = data.calls || {};
  return (
    <main className="page">
      <Stale error={error} loadedAt={loadedAt} />
      <section className="hero page-hero">
        <h1 className="hero-title">机房</h1>
        <div className="hero-lede">
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
          <Lamp state={agy ? (agy.ok && agyMedia?.ok !== false ? "ok" : "bad") : "idle"} label="模型" />
          <div className="big">{agy ? (!agy.ok ? "需要登录" : agyMedia?.ok === false ? "读不了视频" : "在线") : "未探测"}</div>
          <div className="sub"><span className="num">{data.model}</span>{agy?.checked_at ? ` · ${ago(agy.checked_at)}检查` : ""}</div>
          {agyMedia && (
            <div className="sub">读视频自检{agyMedia.ok ? "通过" : "没通过"} · agy <span className="num">{agyMedia.version}</span>
              {agyMedia.checked_at && ` · ${ago(agyMedia.checked_at)}`}</div>
          )}
        </div>
        <div className="inst">
          <Lamp state={ytLamp} label="YouTube 会话" />
          <div className="big">{ytBig}</div>
          <div className="sub">
            {!cc?.configured ? "服务器 Chromium"
              : ccDown ? <>{data.youtube.fix?.[ccs.code] || ccs.code}（错误码 {ccs.code}{ccs.at && ` · ${ago(ccs.at)}`}）这段时间的下载改用服务器上的 Chromium。</>
              : <>CookieCloud{ccs?.at && <> · {ago(ccs.at)}读取</>}{ccs?.ok && <> · <span className="num">{ccs.count}</span> 条</>}</>}
          </div>
          {cc?.configured && vt && (
            <div className="sub">视频测试{vt.ok ? "通过" : "没通过"}{vt.video?.title ? ` · 《${vt.video.title}》` : ""}{vt.at ? ` · ${ago(vt.at)}` : ""}</div>
          )}
          {cc?.configured && yt && !ccDown && <div className="sub">有视频在等 YouTube 登录：做一次视频测试，通过后会立刻重试。</div>}
          {cc?.configured ? <button className="btn sm inst-act" onClick={() => setCcOpen(true)}>配置与测试</button> : (
            <>
              <button className="btn sm inst-act" onClick={() => setCcOpen(true)}>配置 CookieCloud</button>
              {!phone && (
                <details className="fold mini inst-alt">
                  <summary><i className="plus" aria-hidden /><span>在服务器 Chromium 里登录</span></summary>
                  <Cmd text={SSH} />
                  <button className="btn sm inst-act" onClick={async () => { const r = await post("/api/youtube/remote-browser"); if (r.ok) window.open(r.url, "_blank"); else alert(r.output); }}>启动远程登录屏幕</button>
                </details>
              )}
            </>
          )}
        </div>
        <div className="inst">
          <Lamp state={data.notion.token && (data.notion.database || data.notion.parent) ? "ok" : "idle"} label="Notion 交付" />
          <div className="big">{data.notion.token ? (data.notion.database ? "已连接" : "待指定位置") : "未配置"}</div>
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
      {ccOpen && <CookieCloudSheet yt={data.youtube} onClose={closeCc} reload={reload} />}
    </main>
  );
}
