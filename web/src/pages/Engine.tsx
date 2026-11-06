import { useCallback, useEffect, useState } from "react";
import { post } from "../api";
import { deJargon, issueHeadline } from "../cjk";
import { agyAlert, openAgyLogin } from "../components/AgyLogin";
import { Cmd } from "../components/Cmd";
import { CookieCloudSheet } from "../components/CookieCloud";
import { about, big, Columns, HBars, hrs, Kpi, Meter, pct, Spark } from "../components/Figures";
import { BackfillPanel, backfillLine } from "../components/BackfillPanel";
import { AsrCard, asrLamp, DiskKnobs, NotionDefault, ParallelKnobs, TelegramCard, useKnobs } from "../components/Knobs";
import { familyOf, type Left, RouteCells, VENDOR_NAME } from "../components/Routes";
import { Sheet } from "../components/Sheet";
import { Fault, Stale, Unroll } from "../components/States";
import { useMedia, usePoll, useStored } from "../hooks";
import { linkProps, useRestoreScroll } from "../router";
import { ago, bytes, clock, ERRORS, errorLabel, mergeAlerts, PRODUCTION, STOPS, UNAVAILABLE, WAIT } from "../util";

const SSH = "ssh -N -L 6080:127.0.0.1:6080 ovh-investment";
/** stage keys of model calls (providers.stage_key) */
const STEP: Record<string, string> = { understanding: "理解", merge: "合并", knowledge: "知识", article: "文章", selection: "选片", boundary: "边界", review: "预审", compile: "编译", health: "探测", media: "自检" };
const MAIN_STEPS = ["understanding", "merge", "knowledge", "article", "selection", "boundary", "review"];
const FIXES: Record<string, string> = { YOUTUBE_AUTH: "配置与测试", cookiecloud: "配置与测试" };
/** "CLEANUP 完成" → "清理 完成": stage and error codes in log lines read as their Chinese labels */
const humanize = (msg: string, labels: Record<string, string>) =>
  deJargon(msg || "").replace(/https?:\/\/(?:www\.)?youtube\.com\/watch\?v=([\w-]+)\S*/g, "$1").replace(/\b[A-Z][A-Z_]{2,}\b/g, (m) => labels[m] || ERRORS[m] || WAIT[m] || m).replace(/^Worker /, "机房");
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

/** the stages a model does, by the name the settings know them by (providers.stage_key) */
const MODEL_STAGE: Record<string, string> = { UNDERSTAND: "understanding", MERGE: "merge", KNOWLEDGE: "knowledge", ARTICLE: "article", SELECT: "selection", BOUNDARY: "boundary", REVIEW_CHECK: "review" };
const STAGE_STATE: Record<string, string> = { DONE: "完成", RUNNING: "进行中", PENDING: "未开始", SKIPPED: "跳过", WARN: "有告警", FAILED: "失败" };
const LIVE = ["RUNNING", "WAITING", "INTERRUPTED", "QUEUED"];

/** The line an issue goes down, a mark per stage. `family`: what a model stage is painted with (the console); left out,
 *  the line is plain ink (the record: which model did a past stage is the receipt's to say, not today's route). */
function Track({ stages, family }: { stages: any[]; family?: (k: string) => string }) {
  return (
    <span className={`eg-track${family ? " lit" : ""}`} aria-hidden>
      {stages.filter((s) => s.name !== "CLEANUP").map((s) => {
        const k = MODEL_STAGE[s.name];
        return <i key={s.name} className={`${s.status}${k && family ? ` m p-${family(k)}` : ""}`} title={`${s.label} · ${STAGE_STATE[s.status] || s.status}`} />;
      })}
    </span>
  );
}

const refreshIcon = (
  <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" /><path d="M13.5 1.8v3h-3" />
  </svg>
);

/** A quota window as a bar in the model's color: amber when low, vermilion when used up on the group production draws
 *  on. `old`: the last reading of something that no longer answers, greyed; `stale`: from before the window started over. */
function Window({ b, label, production, stale, old }: { b?: Bucket; label: string; production: boolean; stale?: boolean; old?: boolean }) {
  if (!b) return null;
  const weekly = b.window === "weekly";
  if (b.disabled) return <Meter label={label} left={null} tone="off" right="不计" sub="周额度用尽时" />;
  const left = b.remaining;
  const out = left != null && left <= 0;
  const tone = left == null ? "off" : old ? "old" : out && production ? "out" : left < 0.2 ? "low" : "ok";
  return <Meter label={label} left={left} tone={tone} right={stale ? "待更新" : undefined} sub={old ? undefined : resets(b.reset_at, weekly, out)} />;
}

function TileHead({ name, href, title, refresh, busy, note }: { name: string; href?: string; title?: string; refresh: () => void; busy: boolean; note?: string }) {
  return (
    <header className="eg-tile-h">
      <i className="pig" aria-hidden />
      {href ? <h3><a href={href} target="_blank" rel="noreferrer" title={title}>{name}</a></h3> : <h3 title={title}>{name}</h3>}
      <button className={`eg-icon${busy ? " busy" : ""}`} onClick={refresh} disabled={busy} aria-label={`刷新 ${name} 额度`} title={note}>{refreshIcon}</button>
    </header>
  );
}

function Figure({ issues }: { issues?: number | null }) {
  if (issues == null) return null;
  return <p className="eg-tile-fig">约够印 <b>{issues}</b> 期</p>;
}

function GeminiTile({ q, log, runway, version, media, authAlert, refresh, busy }: {
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
  const out = !old && !!prod?.buckets.some((b) => !b.disabled && b.remaining === 0);
  const note = q?.checked_at ? `${ago(q.checked_at)}读取` : undefined;
  return (
    <article className={`eg-tile p-gemini${signedOut || out ? " bad" : ""}${old ? " old" : ""}`} aria-label="Gemini 额度">
      <TileHead name="Gemini" title={`agy${version ? ` ${version}` : ""}`} refresh={refresh} busy={busy} note={note} />
      {!old && <Figure issues={out ? 0 : runway?.issues} />}
      {prod ? (
        <div className="eg-meters">
          <Window b={w("5h")} label="5 小时" production old={old} />
          <Window b={w("weekly")} label="每周" production old={old} />
        </div>
      ) : <p className="eg-tile-none">{signedOut ? "agy 没有登录" : "还没读到额度"}</p>}
      {(signedOut || old || media?.ok === false || (q && !q.ok && !signedOut)) && (
        <footer className="eg-tile-note">
          {signedOut ? <span className="zhu">未登录</span> : q && !q.ok ? <span className="zhu">读取失败</span> : null}
          {media?.ok === false && !old && <span className="zhu">读视频自检没通过</span>}
          {old && q?.checked_at && <span>{ago(q.checked_at)}的读数</span>}
          {signedOut && !authAlert && <button className="btn zhu sm" onClick={openAgyLogin}>登录 agy</button>}
        </footer>
      )}
      {others.length > 0 && (
        <details className="fold mini eg-tile-more">
          <summary><i className="plus" aria-hidden /><span>其他模型组</span></summary>
          {others.map((g) => (
            <div key={g.name} className="eg-meters">
              <h4>{g.name}</h4>
              {[...g.buckets].sort((a, b) => BY_WINDOW(a) - BY_WINDOW(b)).map((b, i) => (
                <Window key={i} b={b} label={b.window === "5h" ? "5 小时" : "每周"} production={false} old={old} />
              ))}
            </div>
          ))}
        </details>
      )}
      {pts.length > 1 && <Spark points={pts} label="近 7 天每周额度的余量" height={44} />}
    </article>
  );
}

const subName = (s: any) => VENDOR_NAME[s.provider] || s.name || s.provider;
const subFamily = (s: any) => ({ codex: "chatgpt", claude: "claude", grok: "grok" } as Record<string, string>)[s.provider] || "other";
const stale = (b?: Bucket) => !!b?.reset_at && new Date(b.reset_at).getTime() < Date.now();
const MAGPIE_DOWN: Record<string, string> = { UNREACHABLE: "连不上 magpie", BAD_KEY: "Key 失效", HELD: "这个 Key 读不到额度", NO_KEY: "没有放 Key", REFUSED: "magpie 拒收" };

/** A subscription read from magpie. Accounts, keys and sign-ins are magpie's: this tile reads them and links there. */
function SubTile({ s, m, runway, waiting, refresh, busy }: {
  s: any; m: any; runway: any; waiting: string[]; refresh: () => void; busy: boolean;
}) {
  const st = m.state || {};
  const err: string | null = st.error || null;
  const held = err === "HELD";
  const fallback = held && s.provider === "codex" ? m.fallback : null;  // the last call's own account of its quota
  const windows: any[] = fallback?.buckets || s.windows || [];
  const signedOut = !!s.signed_out, troubled = !!s.error;
  const down = err === "UNREACHABLE" || err === "BAD_KEY";
  const old = down || troubled;  // what is shown is the last that could be read
  const out = !old && windows.some((b) => b.remaining === 0);
  const bad = signedOut || out || err === "BAD_KEY" || err === "UNREACHABLE";
  const edit = `${m.url}/?view=providers&edit=${encodeURIComponent(s.provider)}`;
  const pts = (s.trend || []).map((x: [string, number]) => [new Date(x[0]).getTime(), x[1]] as [number, number]);
  const read = fallback?.at ? `截至 ${ago(fallback.at)}的调用` : st.read_at ? `${ago(s.read_at || st.read_at)}${old ? "的读数" : "读取"}` : null;
  const said = troubled ? <span className="zhu" title={s.error}>{signedOut ? "账号要重新登录" : "读不到额度"}</span>
    : err ? <span className={down ? "zhu" : undefined} title={st.detail || undefined}>{MAGPIE_DOWN[err] || err}</span>
    : out && waiting.length ? <span>{waiting.join("、")}在等额度</span> : null;
  return (
    <article className={`eg-tile p-${subFamily(s)}${bad ? " bad" : ""}${old ? " old" : ""}`} aria-label={`${subName(s)} 额度`}>
      <TileHead name={subName(s)} href={edit} title={[s.plan, s.user].filter(Boolean).join(" · ") || undefined} refresh={refresh} busy={busy} note={read || undefined} />
      {!old && <Figure issues={out ? 0 : runway?.issues} />}
      {windows.length > 0 ? (
        <div className="eg-meters">
          {windows.map((b) => (
            <Window key={b.window} b={b} label={b.label || (b.window === "5h" ? "5 小时" : b.window === "weekly" ? "每周" : b.window)} production old={old} stale={stale(b)} />
          ))}
        </div>
      ) : <p className="eg-tile-none">{troubled ? "读不到额度" : "还没读到额度"}</p>}
      {said && (
        <footer className="eg-tile-note">
          {said}
          {(old || fallback) && read && <span>{read}</span>}
          {troubled ? <a className="btn sm" href={edit} target="_blank" rel="noreferrer">{signedOut ? "去 magpie 登录" : "去 magpie 看"}</a>
            : held || err === "BAD_KEY" ? <a className="btn sm" href={`${m.url}/?view=gateway`} target="_blank" rel="noreferrer">去改 Key</a> : null}
        </footer>
      )}
      {pts.length > 1 && <Spark points={pts} label="近 7 天每周额度的余量" height={44} />}
    </article>
  );
}

/** The subscriptions no stage uses: quiet, up in a tile of their own once a stage is sent to one */
function OtherSubs({ subs, old }: { subs: any[]; old: boolean }) {
  if (!subs.length) return null;
  return (
    <article className={`eg-tile eg-others${old ? " old" : ""}`} aria-label="其他订阅">
      <header className="eg-tile-h"><h3>其他订阅</h3></header>
      {subs.map((s) => (
        <div key={`${s.provider}|${s.user || ""}`} className={`eg-other p-${subFamily(s)}`}>
          <h4><i className="pig" aria-hidden />{subName(s)}</h4>
          {s.error ? <p className="zhu" title={s.error}>{s.signed_out ? "账号要重新登录" : "读不到额度"}</p> : (
            <div className="eg-meters">
              {s.windows.map((b: any) => (
                <Meter key={b.window} label={b.label} left={b.remaining} tone={old ? "old" : b.remaining === 0 ? "out" : "ok"}
                  sub={!old ? resets(b.reset_at, b.window === "weekly", b.remaining === 0) : undefined} />
              ))}
            </div>
          )}
        </div>
      ))}
    </article>
  );
}

/** The press: what is on it now; at rest, the line lies dark in the colors of who would do each stage. */
function Press({ runs, labels, family }: { runs: any[]; labels: Record<string, string>; family: (k: string) => string }) {
  const live = LIVE.flatMap((p) => runs.filter((r) => r.production === p && r.kind !== "EXPERIMENT"));
  // up to four with room to breathe; more than that, a line each, and all that are at work stay in view
  const dense = live.length > 4;
  const shown = dense ? live.slice(0, Math.max(8, live.filter((r) => r.production === "RUNNING").length)) : live;
  const last = !live.length ? runs.find((r) => r.finished_at && r.kind !== "EXPERIMENT") : null;
  const blank = Object.entries(labels).map(([name, label]) => ({ name, label, status: "PENDING" }));
  const title = (r: any) => (
    <a className="t" {...linkProps(`/issue/${encodeURIComponent(r.source_id)}`)}>{issueHeadline(r.headline, null, r.title || r.source_id)}</a>
  );
  return (
    <div className={`eg-press${dense ? " dense" : ""}`}>
      {shown.map((r) => {
        const stage = r.stages.find((x: any) => x.status === "RUNNING") || r.stages.find((x: any) => x.status === "PENDING");
        const stop = r.production === "WAITING" && STOPS.includes(r.wait_reason);
        return (
          <div key={r.id} className={`eg-press-row ${r.production}`}>
            {title(r)}
            <span className={`m${stop ? " bad" : ""}`}>
              {r.production === "RUNNING" ? stage?.label || PRODUCTION.RUNNING : PRODUCTION[r.production] || r.production}
              {r.wait_reason ? ` · ${WAIT[r.wait_reason] || r.wait_reason}` : ""}
            </span>
            {r.production !== "QUEUED" && <Track stages={r.stages} family={family} />}
          </div>
        );
      })}
      {live.length > shown.length && <p className="eg-press-more">还有 {live.length - shown.length} 期</p>}
      {!shown.length && (
        <div className="eg-press-row idle">
          {last ? title(last) : <span className="t">还没有印过</span>}
          {last && <span className="m">上一期 · {ago(last.finished_at)}</span>}
          <Track stages={blank} family={family} />
        </div>
      )}
    </div>
  );
}

type Lamp = "ok" | "bad" | "idle" | "warn";

/** The box's state, a line each; what sets each up opens from its line, so no form stands open on the page. */
function System({ data, onCc, reload }: { data: any; onCc: () => void; reload: () => void }) {
  const phone = useMedia("(max-width: 900px)");
  const [at, setAt] = useState<string | null>(null);
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
  const kept: string | null = data.youtube.kept_at;  // the last login that worked, gone on with while CookieCloud does not answer
  const [ytLamp, ytBig]: [Lamp, string] = ccDown ? (!kept ? ["bad", "读不到 CookieCloud"] : yt ? ["bad", "上一份也失效了"] : ["warn", `用上一份 · ${ago(kept)}`])
    : yt ? ["bad", "需要登录"]
    : cc?.configured ? (ccs?.ok ? ["ok", "已就绪"] : ["idle", "待验证"]) : data.youtube.profile_ok ? ["ok", "已就绪"] : ["idle", "未配置"];
  const nLamp: Lamp = data.notion.token && (data.notion.database || data.notion.parent) ? "ok" : "idle";
  const nBig = data.notion.token ? (data.notion.database ? "已连接" : "待指定位置") : "未配置";
  const dLamp: Lamp = d.free / d.total < 0.15 ? "bad" : "ok";
  const [aLamp, aBig] = asrLamp(data.asr);
  const tLamp: Lamp = !(tg.token && tg.chat) ? "idle" : tg.ok === false || tg.pending ? "bad" : "ok";
  const tBig = tLamp === "idle" ? (tg.token ? "待接通" : "未接") : tg.pending ? `${tg.pending} 条没发出去` : tg.ok === false ? "发不出去" : "已接通";
  const m = data.magpie || {};
  const offline = (m.state?.subs || []).some((s: any) => s.signed_out && (m.vendors || []).includes(s.provider));
  const mErr = m.state?.error;
  const [mLamp, mBig]: [Lamp, string] = mErr === "NO_KEY" ? ["idle", "未配置"] : mErr === "UNREACHABLE" ? ["bad", "连不上"]
    : mErr === "BAD_KEY" ? ["bad", "Key 失效"] : mErr === "HELD" ? ["warn", "Key 受限"] : offline ? ["bad", "账号掉线"]
    : m.state?.ok ? ["ok", "已连接"] : ["idle", "读取中"];
  const st = m.state || {};
  const r = m.report;
  const bfView = data.backfill;
  const bfLine = backfillLine(bfView);
  const lines: { k: string; name: string; title: string; lamp: Lamp; value: string; panel: React.ReactNode }[] = [
    { k: "youtube", name: "YouTube", title: "YouTube 会话", lamp: ytLamp, value: ytBig, panel: (
      <div className="inst">
        <div className="sub">
          {!cc?.configured ? "服务器 Chromium"
            : ccDown ? <>{kept && !yt && <>下载在用上一份登录态，{ago(kept)}读到的<br /></>}{data.youtube.fix?.[ccs.code] || ccs.code}（错误码 {ccs.code}{ccs.code === "ip_not_allowed" && ccs.detail ? ` · ${ccs.detail}` : ""}{ccs.at && ` · ${ago(ccs.at)}`}）{!kept && "这段时间的下载改用服务器上的 Chromium。"}</>
            : <>CookieCloud{ccs?.at && <> · {ago(ccs.at)}读取</>}{ccs?.ok && <> · <span className="num">{ccs.count}</span> 条</>}</>}
        </div>
        {cc?.configured && vt && <div className="sub">视频测试{vt.ok ? "通过" : "没通过"}{vt.video?.title ? ` · 《${vt.video.title}》` : ""}{vt.at ? ` · ${ago(vt.at)}` : ""}</div>}
        <button className="btn sm inst-act" onClick={() => { setAt(null); onCc(); }}>{cc?.configured ? "配置与测试" : "配置 CookieCloud"}</button>
        {!cc?.configured && !phone && data.youtube.profile && (
          <details className="fold mini inst-alt">
            <summary><i className="plus" aria-hidden /><span>在服务器 Chromium 里登录</span></summary>
            <Cmd text={SSH} />
            <button className="btn sm inst-act" onClick={async () => { const x = await post("/api/youtube/remote-browser"); if (x.ok) window.open(x.url, "_blank"); else alert(x.output); }}>启动远程登录屏幕</button>
          </details>
        )}
      </div>
    ) },
    { k: "notion", name: "Notion", title: "Notion 交付", lamp: nLamp, value: nBig, panel: (
      <div className="inst">
        <div className="sub">{data.notion.token ? (data.notion.database ? "写入课代表数据库；各频道的目标库在频道页设置" : data.notion.parent ? "第一次交付时在指定页面下建课代表数据库" : "频道没有目标库时写入的位置未指定") : "没有放 Notion token"}</div>
        <NotionDefault n={data.notion} reload={reload} />
      </div>
    ) },
    { k: "magpie", name: "Magpie", title: "Magpie", lamp: mLamp, value: mBig, panel: (
      <div className="inst">
        {st.key?.name && <div className="sub">Key「{st.key.name}」{st.key.limited ? " · 受限" : ""}{st.key.read_only ? " · 只读" : ""}</div>}
        {(st.models?.length > 0 || st.version) && <div className="sub">{st.models?.length ? `${st.models.length} 个模型` : ""}{st.version ? ` · ${st.version}` : ""}</div>}
        {mLamp !== "ok" && st.error && <div className="sub zhu">{MAGPIE_DOWN[st.error] || st.error}{st.checked_at ? ` · ${ago(st.checked_at)}` : ""}</div>}
        {r && <div className={`sub${r.ok ? "" : " zhu"}`}>{r.ok ? `Gemini 的调用已上报 ${r.total || 0} 次 · ${ago(r.at)}` : `Gemini 的调用报不上去：${MAGPIE_DOWN[r.error] || r.error}`}</div>}
        <div className="kn-acts">
          <a className="btn sm" href={`${m.url}/?view=providers`} target="_blank" rel="noreferrer">供应商 ↗</a>
          <a className="btn sm" href={`${m.url}/?view=gateway`} target="_blank" rel="noreferrer">网关 ↗</a>
        </div>
      </div>
    ) },
    { k: "disk", name: "磁盘", title: "磁盘", lamp: dLamp, value: `${bytes(d.free)} 可用`, panel: (
      <div className="inst">
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
    ) },
    { k: "slots", name: "并行", title: "并行", lamp: "ok", value: `印 ${kn.worker_slots} · 补录 ${bfView.slots} · 下载 ${kn.download_slots}`, panel: <ParallelKnobs kn={kn} save={save} /> },
    { k: "backfill", name: "补录", title: "补录", ...bfLine, panel: <BackfillPanel kn={kn} view={bfView} save={save} /> },
    { k: "asr", name: "转写", title: "GPU 转写", lamp: aLamp, value: aBig, panel: <AsrCard a={data.asr} reload={reload} /> },
    { k: "notify", name: "通知", title: "Telegram 通知", lamp: tLamp, value: tBig, panel: <TelegramCard tg={tg} save={save} reload={reload} /> },
  ];
  const open = lines.find((l) => l.k === at);
  return (
    <section className="eg-card eg-sys" aria-label="系统">
      <h2 className="eg-h">系统</h2>
      <ul>
        {lines.map((l) => (
          <li key={l.k}>
            <button className={l.lamp} onClick={() => setAt(l.k)} aria-haspopup="dialog">
              <i className={`lamp ${l.lamp}`} aria-hidden />
              <span className="n">{l.name}</span>
              <b className="v">{l.value}</b>
              <span className="chev" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
      {open && (
        <Sheet title={open.title} onClose={() => setAt(null)}>
          <div className="eg-panel">{open.panel}</div>
          {err && <p className="err-line" role="alert">{err}</p>}
        </Sheet>
      )}
    </section>
  );
}

const FAMILY: Record<string, string> = { gemini: "Gemini", chatgpt: "ChatGPT", claude: "Claude", grok: "Grok", other: "其他" };
const FAMILIES = Object.keys(FAMILY);
const share = (x: number) => (x > 0 && x < 0.01 ? "<1%" : pct(x));

/** Tokens by stage, each bar in the colors of the models that spent them; the same model keeps its color everywhere. */
function Tokens({ c }: { c: any }) {
  const fam = c.by_family || {};
  const total = Object.values(fam).reduce((a: number, f: any) => a + f.tokens, 0) as number;
  if (!total) return <p className="eg-none">这段时间没有调用</p>;
  const rows = new Map<string, { key: string; tokens: number; parts: [string, number][] }>();
  for (const f of FAMILIES) {
    for (const x of c.by_stage?.[f] || []) {
      const r = rows.get(x.key) || { key: x.key, tokens: 0, parts: [] as [string, number][] };
      r.tokens += x.tokens;
      r.parts.push([f, x.tokens]);
      rows.set(x.key, r);
    }
  }
  const ranked = [...rows.values()].sort((a, b) => b.tokens - a.tokens);
  const top = ranked[0]?.tokens || 1;
  const present = FAMILIES.filter((k) => fam[k]?.tokens);
  return (
    <>
      <div className="eg-fam">
        <div className="eg-fam-bar" role="img" aria-label={present.map((k) => `${FAMILY[k]} ${share(fam[k].tokens / total)}`).join("，")}>
          {present.map((k) => <i key={k} className={`p-${k}`} style={{ flexGrow: fam[k].tokens }} />)}
        </div>
        <div className="eg-keys">
          {present.map((k) => <span key={k} className={`p-${k}`}><i className="pig" aria-hidden />{FAMILY[k]} <b>{share(fam[k].tokens / total)}</b></span>)}
        </div>
      </div>
      <ol className="hbars eg-stack" aria-label="Token 按工序">
        {ranked.slice(0, 7).map((r) => (
          <li key={r.key} title={r.parts.map(([f, t]) => `${FAMILY[f]} ${big(t)}`).join(" · ")}>
            <span className="hb-l">{STEP[r.key] || r.key}</span>
            <span className="hb-t">
              <span className="eg-stack-bar" style={{ width: `${Math.max(1, (r.tokens / top) * 100)}%` }}>
                {r.parts.map(([f, t]) => <i key={f} className={`p-${f}`} style={{ flexGrow: t }} />)}
              </span>
            </span>
            <span className="hb-v">{share(r.tokens / total)}</span>
          </li>
        ))}
      </ol>
    </>
  );
}

function Stats({ s }: { s: any }) {
  const [win, setWin] = useStored<"7d" | "30d">("az-engine-window", "7d");
  const w = s.windows[win];
  const days: any[] = s.days.slice(win === "7d" ? -7 : 0);
  const c = w.calls;
  const rows = w.per_hour.slice(0, 6);
  const rest = w.per_hour.slice(6).reduce((a: number, x: any) => a + x.minutes, 0);
  const latency = w.latency_median;
  return (
    <section className="eg-card eg-stats" aria-label="统计">
      <header className="eg-card-h">
        <h2 className="eg-h">统计</h2>
        <div className="seg" role="radiogroup" aria-label="时间范围">
          {(["7d", "30d"] as const).map((k) => (
            <button key={k} role="radio" aria-checked={win === k} className={win === k ? "on" : ""} onClick={() => setWin(k)}>{k === "7d" ? "近 7 天" : "近 30 天"}</button>
          ))}
        </div>
      </header>
      <div className="eg-kpis">
        <Kpi label="完成" value={big(w.done)} unit="期" sub={w.partial ? `其中有告警 ${w.partial}` : undefined} />
        <Kpi label="视频" value={hrs(w.video_seconds)} unit="小时" />
        <Kpi label="收录到入库" value={latency == null ? "—" : hrs(latency)} unit={latency == null ? undefined : "小时"} sub="中位" />
        <Kpi label="失败" value={big(w.failed)} unit="期" tone={w.failed ? "zhu" : undefined}
          sub={w.reasons.length ? w.reasons.map((r: any) => `${errorLabel(r.kind)} ${r.n}`).join(" · ") : undefined} />
        <Kpi label="调用" value={big(c.n)} unit="次" sub={c.n ? `失败 ${pct(c.failed / c.n)}` : undefined} />
        <Kpi label="模型时长" value={hrs(c.wall)} unit="小时" />
        <Kpi label="Token" value={big(c.tokens)} />
      </div>
      <div className="eg-charts">
        <div>
          <h4>每天印完的视频</h4>
          <Columns label="每天印完的视频小时" height={150} unit="小时"
            series={[{ name: "视频小时", cls: "s-ink" }]} marks={days.map((d) => d.failed > 0)}
            data={days.map((d) => ({
              key: d.t, label: `${+d.t.slice(5, 7)}/${+d.t.slice(8)}`, values: [d.video_seconds / 3600],
              tip: <><b>{hrs(d.video_seconds)} 小时</b> {+d.t.slice(5, 7)}月{+d.t.slice(8)}日 · 完成 {d.done} 期{d.failed ? <> · <span className="zhu">失败 {d.failed}</span></> : null}</>,
            }))} />
        </div>
        <div>
          <h4>每小时视频，各工序用时</h4>
          <HBars label="每小时视频各工序用时（中位，分钟）"
            rows={[...rows.map((x: any) => ({ key: x.name, label: x.label, value: x.minutes, text: `${x.minutes.toFixed(1)} 分` })),
              ...(rest > 0 ? [{ key: "rest", label: "其余", value: rest, text: `${rest.toFixed(1)} 分`, rest: true }] : [])]} />
        </div>
        <div>
          <h4>Token 花在哪</h4>
          <Tokens c={c} />
        </div>
      </div>
    </section>
  );
}

function RunRow({ r, title, several, reload }: { r: any; title?: React.ReactNode; several?: boolean; reload: () => void }) {
  const [busy, setBusy] = useState(false);
  const bad = r.production === "FAILED" || (r.production === "WAITING" && STOPS.includes(r.wait_reason));
  const act = async (what: string) => {
    setBusy(true);
    try { await post(`/api/runs/${r.id}/${what}`); reload(); } finally { setBusy(false); }
  };
  return (
    <li className="eg-run">
      <span className="eg-run-t">{title}</span>
      <span className="eg-run-id num">{several && r.id.replace(/^.*?-(r\d+)$/, "$1")}{r.origin === "backfill" && <em>补录</em>}</span>
      <Track stages={r.stages} />
      <span className={`eg-run-s${bad ? " bad" : ""}`}>
        {r.production !== "COMPLETE" && <>{PRODUCTION[r.production] || r.production}{r.wait_reason ? ` · ${WAIT[r.wait_reason] || r.wait_reason}` : ""}{r.error_class ? ` · ${errorLabel(r.error_class)}` : ""}</>}
      </span>
      <span className="eg-run-w" title={r.created_at}>{ago(r.created_at)}</span>
      <span className="eg-run-a">
        {(["FAILED", "WAITING", "COMPLETE_PARTIAL"].includes(r.production) || (r.production === "CANCELLED" && r.error_class === "STOPPED")) && r.kind !== "IMPORT" &&
          <button className="btn sm" disabled={busy} onClick={() => act("retry")}>重试</button>}
        {r.production === "FAILED" && UNAVAILABLE[r.error_class] &&
          <button className="btn sm" disabled={busy} onClick={() => act("ignore")}>忽略</button>}
      </span>
    </li>
  );
}

const RUNS_FIRST = 10;

function Runs({ runs, reload }: { runs: any[]; reload: () => void }) {
  const [all, setAll] = useState(false);
  const groups: { sid: string; title: string; prod: any[]; exp: any[] }[] = [];
  // what is in production first, then the rest newest first
  const ordered = [...runs.filter((r) => LIVE.includes(r.production)), ...runs.filter((r) => !LIVE.includes(r.production))];
  for (const r of ordered) {
    let g = groups.find((x) => x.sid === r.source_id);
    if (!g) groups.push((g = { sid: r.source_id, title: issueHeadline(r.headline, null, r.title || r.source_id), prod: [], exp: [] }));
    (r.kind === "EXPERIMENT" ? g.exp : g.prod).push(r);
  }
  return (
    <section className="eg-card eg-runs" aria-label="生产记录">
      <header className="eg-card-h"><h2 className="eg-h">生产记录</h2></header>
      <div className="eg-run-list">
        {(all ? groups : groups.slice(0, RUNS_FIRST)).map((g) => {
          const head = <a {...linkProps(`/issue/${encodeURIComponent(g.sid)}`)}>{g.title}</a>;
          return (
            <section key={g.sid}>
              <ol>
                {g.prod.map((r, i) => <RunRow key={r.id} r={r} title={i === 0 ? head : undefined} several={g.prod.length > 1} reload={reload} />)}
                {!g.prod.length && <li className="eg-run"><span className="eg-run-t">{head}</span></li>}
              </ol>
              {g.exp.length > 0 && (
                <details className="fold mini eg-run-exp">
                  <summary><i className="plus" aria-hidden /><span>{`实验运行 ${g.exp.length} 次`}</span></summary>
                  <ol>{g.exp.map((r) => <RunRow key={r.id} r={r} several reload={reload} />)}</ol>
                </details>
              )}
            </section>
          );
        })}
      </div>
      {groups.length > RUNS_FIRST && (
        <button className="eg-more" onClick={() => setAll(!all)} aria-expanded={all}>{all ? "收起" : `全部 ${groups.length} 期`}</button>
      )}
    </section>
  );
}

export default function Engine() {
  const { data, error, loadedAt, reload } = usePoll<any>("/api/engine", 10000);
  const stats = usePoll<any>("/api/engine/stats", 60000);
  const [ccOpen, setCcOpen] = useState(() => location.hash === "#cc");  // the 首页 links here to fix a login
  const [onlyBad, setOnlyBad] = useStored("az-log-bad", false);
  const [reading, setReading] = useState(false);
  const [readingM, setReadingM] = useState(false);
  const [readErr, setReadErr] = useState<string | null>(null);
  const closeCc = useCallback(() => {
    setCcOpen(false);
    if (location.hash) history.replaceState(history.state, "", location.pathname + location.search);
  }, []);
  const { save: knobSave, err: knobErr } = useKnobs(reload);
  useRestoreScroll(!!data);
  useEffect(() => {
    const on = () => { reload(); stats.reload(); };
    addEventListener("az:refresh", on);
    return () => removeEventListener("az:refresh", on);
  }, [reload, stats.reload]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!data) return <main className="page">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const open = mergeAlerts(data.alerts.filter((a: any) => !a.resolved_at));
  const authAlert = open.some(agyAlert);
  const inUse: string[] = data.providers?.in_use || ["agy"];
  const q = data.quota || {};
  const m = data.magpie || {};
  const mSt = m.state || {};
  const subs: any[] = mSt.subs || [];
  const vendors: string[] = m.vendors || [];
  const models: any[] = mSt.models || [];
  const paused = data.paused;
  const counts = data.counts || {};
  const stages: Record<string, string> = data.providers?.stages || {};
  const main: string = data.providers?.default || "agy";
  const routeOf = (k: string) => stages[k] || main;
  const family = (k: string) => familyOf(routeOf(k), models);
  const vendorOf = (r: string) => models.find((x) => x.id === r)?.vendor ?? (r.includes("/") && !r.startsWith("group/") ? r.split("/")[0] : null);
  const waiting = (v: string) => MAIN_STEPS.filter((k) => vendorOf(routeOf(k)) === v).map((k) => STEP[k]);
  // a tile per subscription a stage draws on, and one saying what is the matter where magpie has told none yet
  const cards: any[] = vendors.flatMap((v) => {
    const own = subs.filter((s) => s.provider === v);
    return own.length ? own : [{ provider: v, name: v, windows: [] }];
  });
  const others = subs.filter((s) => !vendors.includes(s.provider));
  const down = mSt.error === "UNREACHABLE" || mSt.error === "BAD_KEY";
  const agyProduction: Bucket[] = ((q.agy?.groups || []) as Group[]).filter((g) => g.name === q.agy?.production_group).flatMap((g) => g.buckets);
  const agyWindows = inUse.includes("agy") ? agyProduction : [];
  // when a used-up quota comes back: the latest reset among the windows at zero, of the subscriptions in use
  const zero: Bucket[] = [...agyWindows, ...(down ? [] : cards.flatMap((s) => s.windows))].filter((b) => !b.disabled && b.remaining === 0 && b.reset_at);
  const back = zero.map((b) => b.reset_at!).sort().pop();
  // how long the press can go: the tightest subscription in use, and whose it is
  const isOut = (ws: any[]) => ws.some((b) => !b.disabled && b.remaining === 0);
  const runwayKey = (s: any) => `${s.provider}|${(s.user || "").toLowerCase()}`;
  const reach = [
    ...(inUse.includes("agy") && q.agy?.ok ? [{ name: "Gemini", fam: "gemini", out: isOut(agyWindows), issues: stats.data?.runway?.issues as number | undefined }] : []),
    ...(down ? [] : cards.filter((s) => !s.error).map((s) => ({ name: subName(s), fam: subFamily(s), out: isOut(s.windows), issues: stats.data?.runways?.[runwayKey(s)]?.issues as number | undefined }))),
  ].map((x) => (x.out ? { ...x, issues: 0 } : x)).filter((x) => x.issues != null).sort((a, b) => Number(b.out) - Number(a.out) || a.issues! - b.issues!);
  const tight = reach[0];
  const dry = !!tight?.out;  // used up, not merely too little left for a whole issue
  const lost = cards.find((s) => s.signed_out);
  const trouble = vendors.length && down ? MAGPIE_DOWN[mSt.error] : lost ? `${subName(lost)} 账号要重新登录` : dry ? `${tight.name} 额度用完` : m.missing?.length ? "分工的模型已取消" : null;
  // what each subscription has left, for whoever is picking a model: the tightest window, or what stands in its way
  const leftOf = (ws: any[] = []): Left | null => {
    const xs = ws.filter((b) => !b.disabled && b.remaining != null);
    if (!xs.length) return null;
    const low = xs.reduce((a, b) => (b.remaining < a.remaining ? b : a));
    return low.remaining === 0 ? { note: resets(low.reset_at, low.window === "weekly", true) || "用完了", bad: true } : { left: low.remaining };
  };
  const lefts: Record<string, Left | null> = { agy: q.agy?.error === "AGY_AUTH_REQUIRED" ? { note: "没有登录", bad: true } : q.agy?.ok ? leftOf(agyProduction) : null };
  for (const s of subs) {
    const l: Left | null = s.signed_out ? { note: "要重新登录", bad: true } : s.error ? { note: "读不到额度" } : down ? null : leftOf(s.windows);
    const was = lefts[s.provider];
    if (!was || was.left == null || (l?.left ?? 0) > was.left) lefts[s.provider] = l;  // several accounts: the gateway can use the one with the most
  }
  const liveKeys = (data.runs as any[]).filter((r) => r.production === "RUNNING").flatMap((r) => r.stages.filter((x: any) => x.status === "RUNNING").map((x: any) => MODEL_STAGE[x.name])).filter(Boolean);
  const refresh = async () => {
    setReading(true);
    setReadErr(null);
    try { await post("/api/agy/quota"); reload(); stats.reload(); } catch (e) { setReadErr((e as Error).message); }
    setReading(false);
  };
  const refreshM = async () => {
    setReadingM(true);
    setReadErr(null);
    try { await post("/api/magpie/refresh"); reload(); stats.reload(); } catch (e) { setReadErr((e as Error).message); }
    setReadingM(false);
  };
  const events = collapse(data.events).filter((e: any) => !onlyBad || e.level !== "info");
  return (
    <main className="page engine">
      <Stale error={error} loadedAt={loadedAt} />
      <header className="pagehead">
        <h1>机房</h1>
        <p className={`ph-status${paused || trouble ? " bad" : ""}`}>
          <i className={`lamp ${paused || trouble ? "bad" : "ok"}`} aria-hidden />
          {paused ? (
            // the alert below says why and what fixes it; a pause for agy's sign-in is lifted by signing in, not here
            <>队列已暂停{!open.some((a: any) => a.key === paused.reason) && <>{` · ${WAIT[paused.reason] || paused.reason}`}{back && ` · ${resets(back, true, true)}`}</>}
              {!(paused.reason === "AGY_AUTH_REQUIRED" && authAlert) &&
                <button className="btn sm" onClick={async () => { await post("/api/queue/resume"); reload(); }}>手动恢复</button>}</>
          ) : trouble ? (
            <>{trouble}{dry && !lost && back && ` · ${resets(back, true, true)}`}</>
          ) : (
            <>运转正常{data.drain_seconds > 0 && ` · ${about(data.drain_seconds)}印完`}</>
          )}
        </p>
      </header>

      {open.length > 0 && (
        <section className="attn" aria-label="告警">
          {open.map((a: any) => (
            <div className={`attn-row${a.severity === "info" ? "" : " bad"}`} key={a.key} role={a.severity === "info" ? "status" : "alert"}>
              <span className="mark" aria-hidden />
              <div className="attn-t">
                <h4>{a.title}</h4>
                {!agyAlert(a) && <p>{(a.body || "").replace(/`/g, "")}</p>}
              </div>
              {agyAlert(a) ? <button className="btn zhu" onClick={openAgyLogin}>登录 agy</button>
                : FIXES[a.key] ? <button className="btn sm" onClick={() => setCcOpen(true)}>{FIXES[a.key]}</button>
                : a.key === "PROVIDER_MODEL" || a.key === "AGY_AUTH_REQUIRED" ? <a className="btn sm" href={`${m.url}/?view=providers`} target="_blank" rel="noreferrer">打开 magpie</a> : null}
            </div>
          ))}
        </section>
      )}

      <div className="eg-now">
        <section className="eg-console" aria-label="生产线">
          <div className="eg-figs">
            {tight && (
              <div className={`eg-hero p-${tight.fam}${tight.issues === 0 ? " out" : ""}`}>
                <span className="eg-l">约够印</span>
                <b>{tight.issues}<small>期</small></b>
                <span className="eg-who"><i className="pig" aria-hidden />{tight.name}{dry && back && <em>{resets(back, true, true)}</em>}</span>
              </div>
            )}
            <div className="eg-fig"><span className="eg-l">在印</span><b>{counts.running ?? 0}<small>/{data.slots}</small></b></div>
            <div className="eg-fig"><span className="eg-l">排队</span><b>{counts.queued ?? 0}</b></div>
            {data.backfill.pending > 0 && <div className="eg-fig"><span className="eg-l">补录</span><b>{data.backfill.pending}</b></div>}
            <div className={`eg-fig${counts.waiting ? " warn" : ""}`}><span className="eg-l">等待</span><b>{counts.waiting ?? 0}</b></div>
          </div>
          <Press runs={data.runs} labels={data.stage_labels || {}} family={family} />
          <div className="eg-split">
            <h2 className="eg-l">分工</h2>
            <RouteCells kn={data.knobs} steps={MAIN_STEPS} label={STEP} save={knobSave} models={models} routes={{ default: main, stages }} missing={m.missing || []} live={liveKeys} lefts={lefts} />
          </div>
        </section>
        <System data={data} onCc={() => setCcOpen(true)} reload={reload} />
      </div>
      {(readErr || knobErr) && <p className="err-line" role="alert">{readErr || knobErr}</p>}

      <section className="eg-quota" aria-label="额度">
        {inUse.includes("agy") && (
          <GeminiTile q={q.agy} log={q.agy_log || []} runway={stats.data?.runway} version={data.agy_media?.version || q.agy?.version}
            media={data.agy_media} authAlert={authAlert} refresh={refresh} busy={reading} />
        )}
        {cards.map((s) => (
          <SubTile key={runwayKey(s)} s={s} m={m} runway={stats.data?.runways?.[runwayKey(s)]}
            waiting={waiting(s.provider)} refresh={refreshM} busy={readingM} />
        ))}
        <OtherSubs subs={others} old={down || mSt.error === "HELD"} />
      </section>

      {stats.data ? <Stats s={stats.data} /> : null}

      <div className="eg-record">
        <Runs runs={data.runs} reload={reload} />
        <div className="eg-log-slot">
          <section className="eg-card eg-log" aria-label="日志">
            <header className="eg-card-h"><h2 className="eg-h">日志</h2>
              <label className="chk"><input type="checkbox" checked={onlyBad} onChange={(e) => setOnlyBad(e.target.checked)} />只看异常</label>
            </header>
            <ol className="log" role="log">
              {events.map((e: any) => (
                <li key={e.id} className={`lv-${e.level}`}>
                  <span className="when num" title={e.ts}>{clock(e.ts)}</span>
                  <span className="msg">{humanize(e.message, data.stage_labels || {})}{e.n > 1 && <span className="muted"> ×{e.n}</span>}</span>
                </li>
              ))}
              {!events.length && <li className="log-none">{onlyBad ? "没有异常" : "还没有记录"}</li>}
            </ol>
          </section>
        </div>
      </div>
      {ccOpen && <CookieCloudSheet yt={data.youtube} onClose={closeCc} reload={reload} />}
    </main>
  );
}
