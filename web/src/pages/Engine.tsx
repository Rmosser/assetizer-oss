import { useCallback, useEffect, useState } from "react";
import { post } from "../api";
import { deJargon, issueHeadline } from "../cjk";
import { agyAlert, openAgyLogin } from "../components/AgyLogin";
import { Cmd } from "../components/Cmd";
import { CookieCloudSheet } from "../components/CookieCloud";
import { about, big, Columns, HBars, hrs, Kpi, Meter, pct, Spark } from "../components/Figures";
import { Fold } from "../components/Fold";
import { AsrCard, asrLamp, DiskKnobs, NotionDefault, ParallelKnobs, Routing, TelegramCard, useKnobs } from "../components/Knobs";
import { Fault, Stale, Unroll } from "../components/States";
import { useMedia, usePoll, useStored } from "../hooks";
import { linkProps, useRestoreScroll } from "../router";
import { ago, bytes, clock, PRODUCTION, STOPS, WAIT } from "../util";

const SSH = "ssh -N -L 6080:127.0.0.1:6080 ovh-investment";
const KIND: Record<string, string> = { worker: "机房", stage: "工序", run: "付印", delivery: "交付", review: "审阅", compile: "编译", ingest: "收录", alert: "告警", experiment: "实验", config: "配置" };
/** stage keys of model calls (providers.stage_key) */
const STEP: Record<string, string> = { understanding: "理解", merge: "合并", knowledge: "知识", article: "文章", selection: "选片", boundary: "边界", review: "预审", compile: "编译", health: "探测", media: "自检" };
const MAIN_STEPS = ["understanding", "merge", "knowledge", "article", "selection", "boundary", "review"];
const ERRORS: Record<string, string> = {
  PROVIDER_TIMEOUT: "模型超时", PROVIDER_FAILURE: "模型出错", PROVIDER_TRANSIENT: "模型暂时不可用", PROVIDER_OUTPUT_LIMIT: "输出超长",
  MODALITY_ROUTING_DEVIATION: "模型越界读文件", MEDIA_READ_FAILED: "读不了视频", CONTRACT: "输出不合规", DOWNLOAD: "下载失败",
  TOO_MANY_RESTARTS: "反复重启", "?": "未分类",
};
const FIXES: Record<string, string> = { YOUTUBE_AUTH: "配置与测试" };
/** "CLEANUP 完成" → "清理 完成": stage and error codes in log lines read as their Chinese labels */
const humanize = (msg: string, labels: Record<string, string>) =>
  deJargon(msg || "").replace(/\b[A-Z][A-Z_]{2,}\b/g, (m) => labels[m] || ERRORS[m] || WAIT[m] || m).replace(/^Worker /, "机房");
/** consecutive identical events collapse into one line with a count */
const collapse = (events: any[]) => events.reduce((a: any[], e: any) => {
  const l = a[a.length - 1];
  if (l && l.kind === e.kind && l.message === e.message) { l.n++; return a; }
  a.push({ ...e, n: 1 });
  return a;
}, []);
const WEEKDAY = "日一二三四五六";
/** when a window starts over: within a day "2 小时 13 分后", further "周五 07:34"; it 重置 — or, used up, 恢复 */
function resets(iso?: string | null, weekly = false, out = false): string | null {
  if (!iso) return null;
  const t = new Date(iso), s = (t.getTime() - Date.now()) / 1000;
  const verb = out ? "恢复" : "重置";
  if (Number.isNaN(s)) return null;
  if (s <= 0) return `已到${verb}时间`;
  const hm = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  if (weekly || s > 86400) return `${s > 6 * 86400 ? `${t.getMonth() + 1}月${t.getDate()}日` : `周${WEEKDAY[t.getDay()]}`} ${hm} ${verb}`;
  const all = Math.max(1, Math.round(s / 60)), h = Math.floor(all / 60), m = all % 60;  // 7199 s: 2 小时 0 分, not 1 小时 60 分
  return `${h ? `${h} 小时 ` : ""}${m} 分后${verb}`;
}
const BY_WINDOW = (b: Bucket) => (b.window === "5h" ? 0 : b.window === "weekly" ? 1 : 2);

type Bucket = { window: string; remaining: number | null; reset_at: string | null; disabled: boolean };
type Group = { name: string; models: string; buckets: Bucket[] };

function StageBar({ stages }: { stages: any[] }) {
  return (
    <span className="stagebar" aria-label="阶段">
      {stages.filter((s) => s.name !== "CLEANUP").map((s) => <i key={s.name} className={s.status} title={`${s.label} · ${s.status}`} />)}
    </span>
  );
}

function RunRow({ r, reload }: { r: any; reload: () => void }) {
  const bad = r.production === "FAILED" || (r.production === "WAITING" && STOPS.includes(r.wait_reason));
  return (
    <li className="run-row">
      <span className="run-id num">{r.id.replace(/^.*?-(r\d+)$/, "$1")}{r.origin === "backfill" && <em className="run-o">补录</em>}</span>
      <span className="run-when" title={r.created_at}>{ago(r.created_at)}</span>
      <StageBar stages={r.stages} />
      <span className={`run-state${bad ? " bad" : ""}`}>
        {PRODUCTION[r.production] || r.production}{r.wait_reason ? ` · ${WAIT[r.wait_reason] || r.wait_reason}` : ""}{r.error_class ? ` · ${ERRORS[r.error_class] || r.error_class}` : ""}
      </span>
      {["FAILED", "WAITING", "COMPLETE_PARTIAL"].includes(r.production) && r.kind !== "IMPORT" &&
        <button className="btn sm" onClick={async () => { await post(`/api/runs/${r.id}/retry`); reload(); }}>重试</button>}
    </li>
  );
}

function Runs({ runs, reload }: { runs: any[]; reload: () => void }) {
  const groups: { sid: string; title: string; prod: any[]; exp: any[] }[] = [];
  const live = ["RUNNING", "QUEUED", "WAITING", "INTERRUPTED"];
  // what is in production first, then the rest newest first
  const ordered = [...runs.filter((r) => live.includes(r.production)), ...runs.filter((r) => !live.includes(r.production))];
  for (const r of ordered) {
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
              <ol>{g.exp.map((r) => <RunRow key={r.id} r={r} reload={reload} />)}</ol>
            </details>
          )}
        </section>
      ))}
    </div>
  );
}

/** A quota window as a bar: amber when low, vermilion when used up on the group production draws on. `old`: the last
 *  reading of an agy that no longer answers, greyed; `stale`: a reading from before the window started over. */
function Window({ b, label, production, stale, old }: { b?: Bucket; label: string; production: boolean; stale?: boolean; old?: boolean }) {
  if (!b) return null;
  const weekly = b.window === "weekly";
  if (b.disabled) return <Meter label={label} left={null} tone="off" right="不计" sub="周额度用尽时" />;
  const left = b.remaining;
  const out = left != null && left <= 0;
  const tone = left == null ? "off" : old ? "old" : out && production ? "out" : left < 0.2 ? "low" : "ok";
  return <Meter label={label} left={left} tone={tone} right={stale ? "待更新" : undefined} sub={old ? undefined : resets(b.reset_at, weekly, out)} />;
}

function GeminiCard({ q, log, runway, version, media, authAlert, refresh, busy }: {
  q: any; log: [string, number | null, number | null][]; runway: any; version?: string; media: any;
  authAlert: boolean; refresh: () => void; busy: boolean;
}) {
  const groups: Group[] = q?.groups || [];
  const prod = groups.find((g) => g.name === q?.production_group) || groups[0];
  const signedOut = q?.error === "AGY_AUTH_REQUIRED";
  const old = !!q && !q.ok && groups.length > 0;  // agy no longer answers: these are its last numbers
  const others = groups.filter((g) => g !== prod);
  const pts = log.filter((x) => x[2] != null).map((x) => [new Date(x[0]).getTime(), x[2] as number] as [number, number]);
  const w = (k: string) => prod?.buckets.find((b) => b.window === k);
  return (
    <article className={`qcard${signedOut ? " bad" : ""}`} aria-label="Gemini 额度">
      <header className="qc-h">
        <i className={`lamp ${signedOut || media?.ok === false ? "bad" : q?.ok ? "ok" : "idle"}`} aria-hidden />
        <h3>Gemini</h3>
        <span className="qc-src">agy{version ? ` ${version}` : ""}</span>
        <button className="btn ghost sm qc-r" onClick={refresh} disabled={busy}>{busy ? "读取中…" : "刷新"}</button>
      </header>
      {prod ? (
        <>
          <Window b={w("5h")} label="5 小时" production old={old} />
          <Window b={w("weekly")} label="每周" production old={old} />
          {!old && (runway || pts.length > 1) && (
            <div className="qc-run">
              {runway && <span>约够印 <b>{runway.issues}</b> 期</span>}
              {pts.length > 1 && <Spark points={pts} label="近 7 天每周额度的余量" height={30} />}
            </div>
          )}
        </>
      ) : <p className="qc-none">{signedOut ? "agy 没有登录" : "还没读到额度"}</p>}
      <footer className="qc-f">
        {signedOut ? <span className="zhu">未登录</span> : q?.ok ? <span>已登录</span> : q ? <span>读取失败</span> : null}
        {media && !old && <span>读视频自检{media.ok ? "通过" : <b className="zhu">没通过</b>}</span>}
        {q?.checked_at && (old || q.ok) && <span>{ago(q.checked_at)}{old ? "的读数" : "读取"}</span>}
        {signedOut && !authAlert && <button className="btn zhu sm" onClick={openAgyLogin}>登录 agy</button>}
      </footer>
      {others.length > 0 && (
        <details className="fold mini qc-more">
          <summary><i className="plus" aria-hidden /><span>其他模型组</span></summary>
          {others.map((g) => (
            <div key={g.name} className="qc-g">
              <h4>{g.name}</h4>
              {[...g.buckets].sort((a, b) => BY_WINDOW(a) - BY_WINDOW(b)).map((b, i) => (
                <Window key={i} b={b} label={b.window === "5h" ? "5 小时" : "每周"} production={false} old={old} />
              ))}
            </div>
          ))}
        </details>
      )}
    </article>
  );
}

function ChatGptCard({ q }: { q: any }) {
  const now = Date.now();
  const w = (k: string) => q?.buckets?.find((b: Bucket) => b.window === k);
  const stale = (b?: Bucket) => !!b?.reset_at && new Date(b.reset_at).getTime() < now;
  return (
    <article className="qcard" aria-label="ChatGPT 额度">
      <header className="qc-h"><i className={`lamp ${q ? "ok" : "idle"}`} aria-hidden /><h3>ChatGPT</h3><span className="qc-src">magpie</span></header>
      {q ? (
        <>
          <Window b={w("5h")} label="5 小时" production stale={stale(w("5h"))} />
          <Window b={w("weekly")} label="每周" production stale={stale(w("weekly"))} />
        </>
      ) : <p className="qc-none">还没有调用</p>}
      {q?.at && <footer className="qc-f"><span>截至 {ago(q.at)}的调用</span></footer>}
    </article>
  );
}

/** One line of lamps; the details unfold. Folded only while every lamp is fine: something not set up or not working
 *  shows its details (and what sets it up) without a click. */
function Lamps({ data, choice, setOpen, onCc, reload }: { data: any; choice: boolean | null; setOpen: (v: boolean) => void; onCc: () => void; reload: () => void }) {
  const phone = useMedia("(max-width: 900px)");
  const { save, err } = useKnobs(reload);
  const kn = data.knobs;
  const tg = data.telegram;
  const d = data.disk;
  const used = d.total - d.free;
  const pc = (x: number) => `${Math.max(0.4, (x / d.total) * 100)}%`;
  const other = Math.max(0, used - d.media - d.runs - d.packages);
  const yt = data.alerts.find((a: any) => a.key === "YOUTUBE_AUTH" && !a.resolved_at);
  const cc = data.youtube.cookiecloud;
  const ccs = data.youtube.state?.source === "cookiecloud" ? data.youtube.state : null;
  const vt = data.youtube.video_test?.verdict ? data.youtube.video_test : null;
  const ccDown = !!(cc?.configured && ccs && !ccs.ok);  // the cause to show, not the "需要登录" it leads to
  const [ytLamp, ytBig]: ["ok" | "bad" | "idle", string] = ccDown ? ["bad", "读不到 CookieCloud"] : yt ? ["bad", "需要登录"]
    : cc?.configured ? (ccs?.ok ? ["ok", "已就绪"] : ["idle", "待验证"]) : data.youtube.profile_ok ? ["ok", "已就绪"] : ["idle", "未配置"];
  const nLamp = data.notion.token && (data.notion.database || data.notion.parent) ? "ok" : "idle";
  const nBig = data.notion.token ? (data.notion.database ? "已连接" : "待指定位置") : "未配置";
  const dLamp = d.free / d.total < 0.15 ? "bad" : "ok";
  const [aLamp, aBig] = asrLamp(data.asr);
  const tLamp = tg.token && tg.chat ? "ok" : "idle";
  // the last two are optional: not set up, they do not unfold the cards on their own (a failing test does)
  const items: [string, string, string, boolean?][] = [["YouTube", ytLamp, ytBig], ["Notion", nLamp, nBig], ["磁盘", dLamp, `${bytes(d.free)} 可用`],
    ["并行", "ok", `${data.slots} 槽`], ["转写", aLamp, aBig, true], ["通知", tLamp, tLamp === "ok" ? "已接通" : tg.token ? "待接通" : "未接", true]];
  const open = choice ?? items.some(([, lamp, , optional]) => (optional ? lamp === "bad" : lamp !== "ok"));
  return (
    <section className="sys" aria-label="系统">
      <button className="sys-row" aria-expanded={open} onClick={() => setOpen(!open)}>
        {items.map(([k, lamp, v]) => <span key={k} className={`sys-i ${lamp}`}><i className={`lamp ${lamp}`} aria-hidden />{k}<b>{v}</b></span>)}
        <span className={`chev${open ? " up" : ""}`} aria-hidden />
      </button>
      <Fold open={open}>
        <div className="sys-cards">
          <div className="inst">
            <span className="label">YouTube 会话</span>
            <div className="sub">
              {!cc?.configured ? "服务器 Chromium"
                : ccDown ? <>{data.youtube.fix?.[ccs.code] || ccs.code}（错误码 {ccs.code}{ccs.code === "ip_not_allowed" && ccs.detail ? ` · ${ccs.detail}` : ""}{ccs.at && ` · ${ago(ccs.at)}`}）这段时间的下载改用服务器上的 Chromium。</>
                : <>CookieCloud{ccs?.at && <> · {ago(ccs.at)}读取</>}{ccs?.ok && <> · <span className="num">{ccs.count}</span> 条</>}</>}
            </div>
            {cc?.configured && vt && <div className="sub">视频测试{vt.ok ? "通过" : "没通过"}{vt.video?.title ? ` · 《${vt.video.title}》` : ""}{vt.at ? ` · ${ago(vt.at)}` : ""}</div>}
            <button className="btn sm inst-act" onClick={onCc}>{cc?.configured ? "配置与测试" : "配置 CookieCloud"}</button>
            {!cc?.configured && !phone && data.youtube.profile && (
              <details className="fold mini inst-alt">
                <summary><i className="plus" aria-hidden /><span>在服务器 Chromium 里登录</span></summary>
                <Cmd text={SSH} />
                <button className="btn sm inst-act" onClick={async () => { const r = await post("/api/youtube/remote-browser"); if (r.ok) window.open(r.url, "_blank"); else alert(r.output); }}>启动远程登录屏幕</button>
              </details>
            )}
          </div>
          <div className="inst">
            <span className="label">Notion 交付</span>
            <div className="sub">{data.notion.token ? (data.notion.database ? "写入课代表数据库；各频道的目标库在频道页设置" : data.notion.parent ? "第一次交付时在指定页面下建课代表数据库" : "频道没有目标库时写入的位置未指定") : "没有放 Notion token"}</div>
            <NotionDefault n={data.notion} reload={reload} />
          </div>
          <div className="inst">
            <span className="label">磁盘</span>
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
            <DiskKnobs kn={kn} save={save} />
          </div>
          <ParallelKnobs kn={kn} save={save} />
          <AsrCard a={data.asr} reload={reload} />
          <TelegramCard tg={tg} save={save} reload={reload} />
        </div>
        {err && <p className="err-line" role="alert">{err}</p>}
      </Fold>
    </section>
  );
}

function Stats({ s }: { s: any }) {
  const [win, setWin] = useStored<"7d" | "30d">("az-engine-window", "7d");
  const w = s.windows[win];
  const days: any[] = s.days.slice(win === "7d" ? -7 : 0);
  const c = w.calls;
  const fam = c.by_family || {};
  const famTotal = Object.values(fam).reduce((a: number, f: any) => a + f.tokens, 0) as number;
  const rows = w.per_hour.slice(0, 6);
  const rest = w.per_hour.slice(6).reduce((a: number, x: any) => a + x.minutes, 0);
  const latency = w.latency_median;
  return (
    <section className="stats" aria-label="统计">
      <div className="rule-title">
        <h2>统计</h2>
        <div className="seg" role="radiogroup" aria-label="时间范围">
          {(["7d", "30d"] as const).map((k) => (
            <button key={k} role="radio" aria-checked={win === k} className={win === k ? "on" : ""} onClick={() => setWin(k)}>{k === "7d" ? "近 7 天" : "近 30 天"}</button>
          ))}
        </div>
      </div>
      <div className="stats-cols">
        <div className="stats-b">
          <h3>产能</h3>
          <div className="kpis">
            <Kpi label="完成" value={big(w.done)} unit="期" sub={w.partial ? `其中有告警 ${w.partial}` : undefined} />
            <Kpi label="视频" value={hrs(w.video_seconds)} unit="小时" />
            <Kpi label="收录到入库" value={latency == null ? "—" : hrs(latency)} unit={latency == null ? undefined : "小时"} sub="中位" />
            <Kpi label="失败" value={big(w.failed)} unit="期" tone={w.failed ? "zhu" : undefined}
              sub={w.reasons.length ? w.reasons.map((r: any) => `${ERRORS[r.kind] || r.kind} ${r.n}`).join(" · ") : undefined} />
          </div>
          <Columns label="每天印完的视频小时" height={110} unit="小时"
            series={[{ name: "视频小时", cls: "s-ink" }]} marks={days.map((d) => d.failed > 0)}
            data={days.map((d) => ({
              key: d.t, label: `${+d.t.slice(5, 7)}/${+d.t.slice(8)}`, values: [d.video_seconds / 3600],
              tip: <><b>{hrs(d.video_seconds)} 小时</b> {+d.t.slice(5, 7)}月{+d.t.slice(8)}日 · 完成 {d.done} 期{d.failed ? <> · <span className="zhu">失败 {d.failed}</span></> : null}</>,
            }))} />
          <h4>每小时视频，各工序用时</h4>
          <HBars label="每小时视频各工序用时（中位，分钟）"
            rows={[...rows.map((x: any) => ({ key: x.name, label: x.label, value: x.minutes, text: `${x.minutes.toFixed(1)} 分` })),
              ...(rest > 0 ? [{ key: "rest", label: "其余", value: rest, text: `${rest.toFixed(1)} 分`, rest: true }] : [])]} />
        </div>
        <div className="stats-b">
          <h3>模型</h3>
          <div className="kpis">
            <Kpi label="调用" value={big(c.n)} unit="次" sub={c.n ? `失败 ${pct(c.failed / c.n)}` : undefined} />
            <Kpi label="模型时长" value={hrs(c.wall)} unit="小时" />
            <Kpi label="Token" value={big(c.tokens)} />
          </div>
          {famTotal > 0 && (
            <>
              <h4>Token 按模型</h4>
              <div className="fam">
                <div className="fam-bar" aria-hidden>
                  {["gemini", "chatgpt", "other"].filter((k) => fam[k]?.tokens).map((k) => (
                    <i key={k} className={`f-${k}`} style={{ flexGrow: fam[k].tokens }} />
                  ))}
                </div>
                <div className="legend">
                  {["gemini", "chatgpt", "other"].filter((k) => fam[k]?.tokens).map((k) => (
                    <span key={k} className={`f-${k}`}>{{ gemini: "Gemini", chatgpt: "ChatGPT", other: "其他" }[k]} {pct(fam[k].tokens / famTotal)}</span>
                  ))}
                </div>
              </div>
            </>
          )}
          {c.gemini_by_stage.length > 0 && (
            <>
              <h4>Gemini 的 Token 花在哪</h4>
              <HBars label="Gemini 的 Token 按工序"
                rows={c.gemini_by_stage.slice(0, 6).map((x: any) => ({ key: x.key, label: STEP[x.key] || x.key, value: x.share, text: pct(x.share) }))} />
            </>
          )}
        </div>
      </div>
    </section>
  );
}

export default function Engine() {
  const { data, error, loadedAt, reload } = usePoll<any>("/api/engine", 10000);
  const stats = usePoll<any>("/api/engine/stats", 60000);
  const [ccOpen, setCcOpen] = useState(false);
  const [sysOpen, setSysOpen] = useState<boolean | null>(null);
  const [onlyBad, setOnlyBad] = useStored("az-log-bad", false);
  const [reading, setReading] = useState(false);
  const [readErr, setReadErr] = useState<string | null>(null);
  const closeCc = useCallback(() => setCcOpen(false), []);
  const { save: knobSave, err: knobErr } = useKnobs(reload);
  useRestoreScroll(!!data);
  useEffect(() => {
    const on = () => { reload(); stats.reload(); };
    addEventListener("az:refresh", on);
    return () => removeEventListener("az:refresh", on);
  }, [reload, stats.reload]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!data) return <main className="page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const open = data.alerts.filter((a: any) => !a.resolved_at);
  const authAlert = open.some(agyAlert);
  const inUse: string[] = data.providers?.in_use || ["agy"];
  const q = data.quota || {};
  const paused = data.paused;
  const counts = data.counts || {};
  // when a used-up quota comes back: the latest reset among the windows at zero, of the providers in use
  const zero: Bucket[] = [
    ...(inUse.includes("agy") ? ((q.agy?.groups || []) as Group[]).filter((g) => g.name === q.agy?.production_group).flatMap((g) => g.buckets) : []),
    ...(inUse.includes("magpie") ? q.magpie?.buckets || [] : []),
  ].filter((b) => !b.disabled && b.remaining === 0 && b.reset_at);
  const back = zero.map((b) => b.reset_at!).sort().pop();
  const routed: Record<string, string> = data.providers?.stages || {};
  const name = (p: string) => ({ agy: "Gemini", tuzi: "Gemini（兔子）", magpie: "ChatGPT" }[p] || p);
  const share: Record<string, string[]> = {};
  for (const k of MAIN_STEPS) (share[routed[k] || data.providers?.default || "agy"] ||= []).push(STEP[k]);
  const refresh = async () => {
    setReading(true);
    setReadErr(null);
    try { await post("/api/agy/quota"); reload(); stats.reload(); } catch (e) { setReadErr((e as Error).message); }
    setReading(false);
  };
  const events = collapse(data.events).filter((e: any) => !onlyBad || e.level !== "info");
  return (
    <main className="page engine">
      <Stale error={error} loadedAt={loadedAt} />
      <header className="pagehead">
        <h1>机房</h1>
        <p className={`ph-status${paused ? " bad" : ""}`}>
          <i className={`lamp ${paused ? "bad" : "ok"}`} aria-hidden />
          {paused ? (
            // the alert below says why and what fixes it; a pause for agy's sign-in is lifted by signing in, not here
            <>队列已暂停{!open.some((a: any) => a.key === paused.reason) && ` · ${WAIT[paused.reason] || paused.reason}`}
              {back && ` · ${resets(back, true, true)}`}
              {!(paused.reason === "AGY_AUTH_REQUIRED" && authAlert) &&
                <button className="btn sm" onClick={async () => { await post("/api/queue/resume"); reload(); }}>手动恢复</button>}</>
          ) : (
            <>运转正常 · 在印 {counts.running}/{data.slots}{counts.queued ? ` · 排队 ${counts.queued}` : ""}{counts.waiting ? ` · 等待 ${counts.waiting}` : ""}
              {data.drain_seconds > 0 && ` · ${about(data.drain_seconds)}印完`}</>
          )}
        </p>
      </header>

      {open.length > 0 && (
        <section className="attn" aria-label="告警">
          {open.map((a: any) => (
            <div className="attn-row bad" key={a.key} role="alert">
              <span className="mark" aria-hidden />
              <div className="attn-t">
                <h4>{a.title}</h4>
                {!agyAlert(a) && <p>{(a.body || "").replace(/`/g, "")}</p>}
              </div>
              {agyAlert(a) ? <button className="btn zhu" onClick={openAgyLogin}>登录 agy</button>
                : FIXES[a.key] ? <button className="btn sm" onClick={() => setCcOpen(true)}>{FIXES[a.key]}</button> : null}
            </div>
          ))}
        </section>
      )}

      <section className="quota-sec" aria-label="额度">
        <div className="rule-title"><h2>额度</h2>
          <span className="aside routes">{Object.entries(share).map(([p, ks]) => <span key={p}>{name(p)}：{ks.join(" · ")}</span>)}</span>
        </div>
        <div className="quota">
          {inUse.includes("agy") && (
            <GeminiCard q={q.agy} log={q.agy_log || []} runway={stats.data?.runway} version={data.agy_media?.version || q.agy?.version}
              media={data.agy_media} authAlert={authAlert} refresh={refresh} busy={reading} />
          )}
          {inUse.includes("magpie") && <ChatGptCard q={q.magpie} />}
        </div>
        <Routing kn={data.knobs} steps={MAIN_STEPS} label={STEP} save={knobSave} />
        {(readErr || knobErr) && <p className="err-line" role="alert">{readErr || knobErr}</p>}
      </section>

      <Lamps data={data} choice={sysOpen} setOpen={setSysOpen} onCc={() => setCcOpen(true)} reload={reload} />

      {stats.data ? <Stats s={stats.data} /> : null}

      <div className="cols2">
        <div>
          <div className="rule-title"><h2>生产记录</h2></div>
          <Runs runs={data.runs} reload={reload} />
        </div>
        <div>
          <div className="rule-title"><h2>日志</h2>
            <label className="chk aside-r"><input type="checkbox" checked={onlyBad} onChange={(e) => setOnlyBad(e.target.checked)} />只看异常</label>
          </div>
          <ol className="log" role="log">
            {events.map((e: any) => (
              <li key={e.id} className={`lv-${e.level}`}>
                <span className="when num" title={e.ts}>{clock(e.ts)}</span>
                <span className="kind">{KIND[e.kind] || e.kind}</span>
                <span className="msg">{humanize(e.message, data.stage_labels || {})}{e.n > 1 && <span className="muted"> ×{e.n}</span>}</span>
              </li>
            ))}
            {!events.length && <li className="log-none">没有异常</li>}
          </ol>
        </div>
      </div>
      {ccOpen && <CookieCloudSheet yt={data.youtube} onClose={closeCc} reload={reload} />}
    </main>
  );
}
