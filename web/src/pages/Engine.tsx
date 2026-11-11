import { useEffect, useState } from "react";
import { post } from "../api";
import { agyGroups, alertFix, boxLines, type Bucket, creditText, type Group, groupName, MAGPIE_DOWN, MAIN_STEPS, onCredits, resets, STEP, subFamily, subName } from "../box";
import { deJargon, issueHeadline } from "../cjk";
import { agyAlert, openAgyLogin } from "../components/AgyLogin";
import { about, big, Columns, HBars, hrs, Kpi, Meter, pct, Spark } from "../components/Figures";
import { familyOf, RouteLinks } from "../components/Routes";
import { Fault, Stale, Unroll } from "../components/States";
import { usePoll, useStored } from "../hooks";
import { linkProps, navigate, useRestoreScroll } from "../router";
import { ago, clock, ERRORS, errorLabel, failText, mergeAlerts, PRODUCTION, retryText, STOPS, WAIT } from "../util";

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
const BY_WINDOW = (b: Bucket) => (b.window === "5h" ? 0 : b.window === "weekly" ? 1 : 2);

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
function Window({ b, label, production, stale, old, credits }: { b?: Bucket; label: string; production: boolean; stale?: boolean; old?: boolean; credits?: boolean }) {
  if (!b) return null;
  const weekly = b.window === "weekly";
  if (b.disabled) return <Meter label={label} left={null} tone="off" right="不计" sub="周额度用尽时" />;
  const left = b.remaining;
  const out = left != null && left <= 0;
  if (out && credits) return <p className="eg-row"><span>{label}</span>{resets(b.reset_at, weekly, true)}</p>;  // the credits carry on: only when it is back
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
  return <p className="eg-tile-fig">约够印 <b>{big(issues)}</b> 期</p>;
}

/** A group of agy's quota a stage draws on (agyGroups), or, with `g` left out, Gemini before one is known. Only the
 *  `first` says how agy itself is doing and folds in the Gemini groups no stage uses (`others`): agy's Claude and GPT
 *  models are none of 课代表's, which takes only Gemini from agy. */
function GeminiTile({ q, g, first, others, log, runway, version, media, authAlert, refresh, busy }: {
  q: any; g?: Group; first: boolean; others: Group[]; log: [string, number | null, number | null][]; runway: any; version?: string; media: any;
  authAlert: boolean; refresh: () => void; busy: boolean;
}) {
  const groups: Group[] = q?.groups || [];
  const name = groupName(g);
  const signedOut = q?.error === "AGY_AUTH_REQUIRED";
  const old = !!q && !q.ok && groups.length > 0;  // agy no longer answers: these are its last numbers
  const pts = log.filter((x) => x[2] != null).map((x) => [new Date(x[0]).getTime(), x[2] as number] as [number, number]);
  const w = (k: string) => g?.buckets.find((b) => b.window === k);
  const out = !old && !!g?.buckets.some((b) => !b.disabled && b.remaining === 0);
  const note = q?.checked_at ? `${ago(q.checked_at)}读取` : undefined;
  return (
    <article className={`eg-tile p-gemini${signedOut || out ? " bad" : ""}${old ? " old" : ""}`} aria-label={`${name} 额度`}>
      <TileHead name={name} title={`agy${version ? ` ${version}` : ""}`} refresh={refresh} busy={busy} note={note} />
      {!old && <Figure issues={out ? 0 : runway?.issues} />}
      {g ? (
        <div className="eg-meters">
          <Window b={w("5h")} label="5 小时" production old={old} />
          <Window b={w("weekly")} label="周额度" production old={old} />
        </div>
      ) : <p className="eg-tile-none">{signedOut ? "agy 没有登录" : groups.length ? "认不出在用的组" : "还没读到额度"}</p>}
      {first && (signedOut || old || media?.ok === false || (q && !q.ok && !signedOut)) && (
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
          {others.map((x) => (
            <div key={x.name} className="eg-meters">
              <h4>{x.name}</h4>
              {[...x.buckets].sort((a, b) => BY_WINDOW(a) - BY_WINDOW(b)).map((b, i) => (
                <Window key={i} b={b} label={b.window === "5h" ? "5 小时" : "周额度"} production={false} old={old} />
              ))}
            </div>
          ))}
        </details>
      )}
      {pts.length > 1 && <Spark points={pts} label="近 7 天每周额度的余量" height={44} />}
    </article>
  );
}

const stale = (b?: Bucket) => !!b?.reset_at && new Date(b.reset_at).getTime() < Date.now();

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
  const credits = !old && !fallback && onCredits(s);  // used up, going on on the account's credits
  const out = !old && !credits && windows.some((b) => b.remaining === 0);
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
      {credits && runway?.issues == null ? <p className="eg-tile-fig">积分 <b>{creditText(s.credits)}</b></p> : !old && <Figure issues={out ? 0 : runway?.issues} />}
      {windows.length > 0 ? (
        <div className="eg-meters">
          {credits && runway?.issues != null && <p className="eg-row"><span>积分</span><b>{creditText(s.credits)}</b></p>}
          {windows.map((b) => (
            <Window key={b.window} b={b} label={b.label || (b.window === "5h" ? "5 小时" : b.window === "weekly" ? "周额度" : b.window)} production old={old} stale={stale(b)} credits={credits} />
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
                <Meter key={b.window} label={b.label} left={b.remaining} tone={old ? "old" : b.remaining === 0 ? (onCredits(s) ? "low" : "out") : "ok"}
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
              {r.production === "RUNNING" && r.retry ? ` · ${retryText(r.retry)}` : ""}
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

const SYSTEM: [keyof ReturnType<typeof boxLines>, string][] = [["youtube", "YouTube"], ["notion", "Notion"], ["magpie", "Magpie"], ["disk", "磁盘"], ["slots", "并行"], ["backfill", "补录"], ["asr", "转写"], ["notify", "通知"]];

/** The box's state, a line each; each goes to where it is set. */
function System({ data }: { data: any }) {
  const lines = boxLines(data);
  return (
    <section className="eg-card eg-sys" aria-label="系统">
      <h2 className="eg-h">系统</h2>
      <ul>
        {SYSTEM.map(([k, name]) => (
          <li key={k}>
            <a className={lines[k].lamp} {...linkProps(`/settings#${k}`)}>
              <i className={`lamp ${lines[k].lamp}`} aria-hidden />
              <span className="n">{name}</span>
              <b className="v">{lines[k].value}</b>
              <span className="chev" aria-hidden />
            </a>
          </li>
        ))}
      </ul>
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
  const tried = Object.values(w.retries || {}).reduce((a: number, x: any) => a + (x.tried || 0), 0);
  // kinds the page names alike are one reason (SCHEMA_VIOLATION, MISSING_UNITS…: 输出不合规)
  const why = new Map<string, number>();
  for (const r of w.reasons) why.set(errorLabel(r.kind), (why.get(errorLabel(r.kind)) || 0) + r.n);
  const reasons = [...why].sort((a, b) => b[1] - a[1]).slice(0, 3);
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
          sub={reasons.length ? reasons.map(([label, n]) => `${label} ${n}`).join(" · ") : undefined} />
        <Kpi label="调用" value={big(c.n)} unit="次" sub={c.n ? `失败 ${pct(c.failed / c.n)}${tried ? ` · 重试 ${big(tried)}` : ""}` : undefined} />
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
        {r.production !== "COMPLETE" && <>{PRODUCTION[r.production] || r.production}{r.wait_reason ? ` · ${WAIT[r.wait_reason] || r.wait_reason}` : ""}{r.error_class ? ` · ${failText(r.error_class, r.retry)}` : r.production === "RUNNING" && r.retry ? ` · ${retryText(r.retry)}` : ""}</>}
      </span>
      <span className="eg-run-w" title={r.created_at}>{ago(r.created_at)}</span>
      <span className="eg-run-a">
        {(["FAILED", "WAITING", "COMPLETE_PARTIAL"].includes(r.production) || (r.production === "CANCELLED" && r.error_class === "STOPPED")) && r.kind !== "IMPORT" &&
          <button className="btn sm" disabled={busy} onClick={() => act("retry")}>重试</button>}
        {r.production === "FAILED" &&
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
  const [onlyBad, setOnlyBad] = useStored("az-log-bad", false);
  const [reading, setReading] = useState(false);
  const [readingM, setReadingM] = useState(false);
  const [readErr, setReadErr] = useState<string | null>(null);
  useRestoreScroll(!!data);
  useEffect(() => { if (location.hash === "#cc") navigate("/settings#cc", { replace: true }); }, []);  // where this used to be
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
  const models: any[] = [...(data.agy_models || []), ...(mSt.models || [])];
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
  const agyUsed = inUse.includes("agy") ? agyGroups(q.agy) : [];  // a tile each: one group's quota is not another's
  const agyWindows = agyUsed.flatMap((g) => g.buckets);
  // when a used-up quota comes back: the latest reset among the windows at zero, of the subscriptions in use
  const zero: Bucket[] = [...agyWindows, ...(down ? [] : cards.filter((s) => !onCredits(s)).flatMap((s) => s.windows))].filter((b) => !b.disabled && b.remaining === 0 && b.reset_at);
  const back = zero.map((b) => b.reset_at!).sort().pop();
  // how long the press can go: the tightest subscription in use, and whose it is
  const isOut = (ws: any[]) => ws.some((b) => !b.disabled && b.remaining === 0);
  const runwayKey = (s: any) => `${s.provider}|${(s.user || "").toLowerCase()}`;
  const reach = [
    ...(q.agy?.ok ? agyUsed.map((g) => ({ name: groupName(g), fam: "gemini", out: isOut(g.buckets), issues: stats.data?.agy_runways?.[g.name]?.issues as number | undefined })) : []),
    ...(down ? [] : cards.filter((s) => !s.error).map((s) => ({ name: subName(s), fam: subFamily(s), out: !onCredits(s) && isOut(s.windows), issues: stats.data?.runways?.[runwayKey(s)]?.issues as number | undefined }))),
  ].map((x) => (x.out ? { ...x, issues: 0 } : x)).filter((x) => x.issues != null).sort((a, b) => Number(b.out) - Number(a.out) || a.issues! - b.issues!);
  const tight = reach[0];
  const dry = !!tight?.out;  // used up, not merely too little left for a whole issue
  const lost = cards.find((s) => s.signed_out);
  const trouble = vendors.length && down ? MAGPIE_DOWN[mSt.error] : lost ? `${subName(lost)} 账号要重新登录` : dry ? `${tight.name} 额度用完` : m.missing?.length ? "分工的模型已取消" : null;
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
          {open.map((a: any) => {
            const fix = alertFix(a);
            return (
              <div className={`attn-row${a.severity === "info" ? "" : " bad"}`} key={a.key} role={a.severity === "info" ? "status" : "alert"}>
                <span className="mark" aria-hidden />
                <div className="attn-t">
                  <h4>{a.title}</h4>
                  {!agyAlert(a) && <p>{(a.body || "").replace(/`/g, "")}</p>}
                </div>
                {agyAlert(a) ? <button className="btn zhu" onClick={openAgyLogin}>登录 agy</button>
                  : fix ? <a className="btn sm" {...linkProps(fix.href)}>{fix.label}</a>
                  : a.key === "AGY_AUTH_REQUIRED" ? <a className="btn sm" href={`${m.url}/?view=providers`} target="_blank" rel="noreferrer">打开 magpie</a> : null}
              </div>
            );
          })}
        </section>
      )}

      <div className="eg-now">
        <section className="eg-console" aria-label="生产线">
          <div className="eg-figs">
            {tight && (
              <div className={`eg-hero p-${tight.fam}${tight.issues === 0 ? " out" : ""}`}>
                <span className="eg-l">约够印</span>
                <b>{big(tight.issues)}<small>期</small></b>
                <span className="eg-who"><i className="pig" aria-hidden />{tight.name}{dry && back && <em>{resets(back, true, true)}</em>}</span>
              </div>
            )}
            <div className="eg-fig"><span className="eg-l">在印</span><b>{counts.running ?? 0}<small>/{data.slots}</small></b></div>
            <div className="eg-fig"><span className="eg-l">排队</span><b>{counts.queued ?? 0}</b></div>
            {data.backfill.pending > 0 && <div className="eg-fig"><span className="eg-l">补录</span><b>{data.backfill.pending}</b></div>}
            {data.topics?.reading > 0 && <div className="eg-fig"><span className="eg-l">专题</span><b>{data.topics.reading}</b></div>}
            <div className={`eg-fig${counts.waiting ? " warn" : ""}`}><span className="eg-l">等待</span><b>{counts.waiting ?? 0}</b></div>
          </div>
          <Press runs={data.runs} labels={data.stage_labels || {}} family={family} />
          <div className="eg-split">
            <h2 className="eg-l">分工</h2>
            <RouteLinks steps={MAIN_STEPS} label={STEP} models={models} routes={{ default: main, stages }} missing={m.missing || []} live={liveKeys} efforts={data.knobs?.efforts} />
          </div>
        </section>
        <System data={data} />
      </div>
      {readErr && <p className="err-line" role="alert">{readErr}</p>}

      <section className="eg-quota" aria-label="额度">
        {inUse.includes("agy") && (agyUsed.length ? agyUsed : [undefined]).map((g, i) => (
          <GeminiTile key={g?.name ?? ""} q={q.agy} g={g} first={i === 0}
            others={i === 0 ? ((q.agy?.groups || []) as Group[]).filter((x) => !agyUsed.includes(x) && /gemini/i.test(`${x.name} ${x.models}`)) : []}
            log={(g && q.agy_log?.[g.name]) || []} runway={g && stats.data?.agy_runways?.[g.name]} version={data.agy_media?.version || q.agy?.version}
            media={data.agy_media} authAlert={authAlert} refresh={refresh} busy={reading} />
        ))}
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
    </main>
  );
}
