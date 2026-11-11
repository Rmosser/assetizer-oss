import { paceText } from "../../box";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type ChannelBackup, gdrive, get, post } from "../../api";
import { IssueTitle } from "../../cjk";
import { linkProps } from "../../router";
import { usePoll } from "../../hooks";
import { Row } from "../../pages/Issues";
import { BACKFILL_BAD, BACKFILL_STOP, bytes, parseDay, WAIT } from "../../util";
import { about, big, Columns, hrs, Kpi, pct, ymd } from "../Figures";
import { Sheet, type SheetHandle } from "../Sheet";
import { Fault } from "../States";
import { Days } from "./Days";
import { TRASH_HASH } from "../GDrive";
import { BackupSegs } from "./LockSheet";
import { fmt, hm, nextScan, Stepper, Switch } from "./bits";
import type { Mode } from "./Header";
import type { Board as BoardData, Candidate, Channel, Scan } from "./types";

/* 看板: one locked channel day to day — what it has produced, and the three ways videos come in: 试一期 (the newest
   one, now), 收录 (each new one, by itself) and 补录 (older ones, by date). */

const SPAN = 16;  // weeks of columns, as on the 频道 overview
const md = (iso?: string | null) => {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(+d) ? `${d.getMonth() + 1}月${d.getDate()}日 ${hm(iso)}` : "";
};
const age = (c: Candidate) => {
  const d = c.published ? new Date(c.published) : null;
  if (!d || Number.isNaN(+d)) return "";
  if (!c.approx) return `${d.getMonth() + 1}月${d.getDate()}日`;
  const days = Math.max(0, Math.round((Date.now() - d.getTime()) / 86400000));
  return days < 1 ? "今天" : days < 7 ? `${days} 天前` : days < 30 ? `约 ${Math.round(days / 7)} 周前` : days < 365 ? `约 ${Math.round(days / 30)} 个月前` : `约 ${Math.round(days / 365)} 年前`;
};
const when = (it: any) => (parseDay(it.published_at) || parseDay(it.ready_at) || parseDay(it.updated_at))?.getTime() ?? 0;
/** not here yet in any form: neither in the journal, the library, the 补录 line, nor a sample */
const fresh = (c: Candidate) => !c.here && !c.library && !c.queued && !c.sample;
/** what one scan found, the numbers that are not zero */
const found = (s: Scan) => !s.found ? "无新视频" : [`发现 ${s.found}`, s.queued && `新收 ${s.queued}`, s.have && `已有 ${s.have}`,
  s.registered && `已登记 ${s.registered}`, s.short && `太短 ${s.short}`].filter(Boolean).join(" · ");
/** what keeps the channel's members-only videos from coming in, as the last scan saw it (ingest.MEMBERS) */
const MEMBERS: Record<string, string> = {
  rss: "只读到公开视频，会员视频会漏收", rejected: "YouTube 登录态失效，会员视频下不了",
  not_signed_in: "CookieCloud 里 YouTube 未登录，会员视频下不了", cookiecloud: "读不到 CookieCloud，会员视频可能下不了",
  none: "没有 YouTube 登录态，会员视频下不了",
};

/** 试一期: the newest video not here yet, produced now (not at the next round, and older than intake reaches). */
function Try({ ch, flash, onDone }: { ch: Channel; flash: (text: string) => void; onDone: () => void }) {
  const [c, setC] = useState<Candidate | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    get<{ candidates: Candidate[] }>(`/api/channels/${encodeURIComponent(ch.id)}/candidates?limit=5`)
      .then((d) => setC(d.candidates.find((x) => fresh(x) && !x.short) || null)).catch(() => setC(null));
  }, [ch.id]);
  useEffect(load, [load]);
  if (!c) return null;  // nothing new to try (or the feed cannot be read: 收录 says so)
  const go = async () => {
    setBusy(true);
    try {
      const r = await post<{ queued: number }>(`/api/channels/${encodeURIComponent(ch.id)}/backfill`, {
        now: true, videos: [{ id: c.id, title: c.title || "", duration: c.duration ?? null }] });
      flash(r.queued ? "已加入付印队列" : "库里已有这一期，没有重复生产");
      onDone();
      load();
    } catch (e) { flash((e as Error).message); }
    setBusy(false);
  };
  return (
    <section className="ch-bp ch-bt" aria-label="试一期">
      <h3>试一期</h3>
      <span className="d">最新未收 · {age(c)}{c.duration ? ` · ${fmt(c.duration)}` : ""}</span>
      <b><IssueTitle title={c.title || c.id} /></b>
      <button className="ch-btn sm solid" disabled={busy} onClick={go}>试这一期</button>
    </section>
  );
}

/** 网盘备份 of the channel's author, the 定稿单's line kept on the 看板: saved as it is clicked. Turned to 不传 with copies
 *  of it no rule wants any more, it says how much can go and opens 可删除 (不传 only stops what is to come). */
function Backup({ cid }: { cid: string }) {
  const [b, setB] = useState<ChannelBackup | null>(null);
  const [left, setLeft] = useState<{ videos: number; bytes: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const turn = useRef(0);  // clicks made: an answer, or a read, asked before the last one is out of date
  const chain = useRef<Promise<unknown>>(Promise.resolve());
  useEffect(() => {
    let alive = true;
    gdrive.channel(cid).then((x) => { if (alive) setB(x); }, () => { /* no line: nothing to set here */ });
    return () => { alive = false; };
  }, [cid]);
  if (!b?.ready) return null;
  // shown at once, sent one after another (the box takes the clicks in their order); the last click's answer is shown
  const set = (f: "member" | "public", v: boolean) => {
    const mine = ++turn.current;
    setB((x) => x && { ...x, [f]: v });
    setErr(null);
    setLeft(null);
    chain.current = chain.current.then(() => gdrive.setChannel(cid, { [f]: v })).then((x) => {
      if (mine !== turn.current) return;
      setB(x);
      if (!v && x.deletable?.videos) setLeft(x.deletable);
    }, (e) => {
      if (mine !== turn.current) return;
      setErr((e as Error).message);
      const at = turn.current;
      gdrive.channel(cid).then((x) => { if (at === turn.current) setB(x); }, () => {});  // what it is now, unless clicked since
    });
  };
  return (
    <>
      <div className="ch-row2 ch-bbk">
        <span className="t">网盘备份</span>
        <BackupSegs name="网盘备份" member={b.member ?? null} pub={b.public ?? null} onChange={set} />
      </div>
      {left && <p className="ch-bbn">已关 · 可删 {left.videos} 期 {bytes(left.bytes)} · <a {...linkProps(`/settings${TRASH_HASH}`)}>去删除</a></p>}
      {err && <p className="ch-err">{err}</p>}
    </>
  );
}

function Intake({ ch, mode, b, onSet, onScan }: {
  ch: Channel; mode: Mode; b: BoardData; onSet: (enabled: boolean, min: number) => Promise<void>; onScan: () => void;
}) {
  const ing = b.ingest;
  const can = mode === "locked" && ch.mode === "TRACKED" && !!ch.youtube_channel_id;
  const [min, setMin] = useState(ing.min_minutes ?? 10);
  const [err, setErr] = useState<string | null>(null);
  const t = useRef<number | undefined>(undefined);
  // what the switch was last set to, ahead of the board's reload: the minimum's delayed save must not undo it
  const want = useRef(!!ing.enabled);
  useEffect(() => { setMin(ing.min_minutes ?? 10); }, [ing.min_minutes]);
  useEffect(() => { want.current = !!ing.enabled; }, [ing.enabled]);
  const send = (en: boolean, m: number) => {
    clearTimeout(t.current);  // a minimum still waiting to be saved goes with this
    want.current = en;
    onSet(en, m).catch((e) => setErr((e as Error).message));
  };
  const scans = ing.scans || [];
  const last = scans[0];
  const got = scans.find((s) => s.queued > 0);
  const broke = last?.error ? last : null;  // only while it lasts: a scan that worked since has cleared it
  const since = ing.since ? new Date(ing.since) : null;
  const on = can && !!ing.enabled;
  const mem = last?.members;
  return (
    <section className="ch-bp" aria-label="收录">
      <h3>收录</h3>
      <div className="ch-bsw" title={on && since && !Number.isNaN(+since) ? `自 ${since.getMonth() + 1} 月 ${since.getDate()} 日起` : undefined}>
        <Switch on={on} disabled={!can} label="自动收录新视频" onChange={(v) => send(v, min)} /><span>自动收录新视频</span>
      </div>
      <div className="ch-row2">
        <span>{min ? "短于" : "时长"}</span>
        <Stepper label="最短时长" zero="不限" value={min} onChange={(v) => { setMin(v); clearTimeout(t.current); t.current = window.setTimeout(() => send(want.current, v), 600); }} />
        {min > 0 && <span>分钟的不收</span>}
      </div>
      {mode === "locked" && <Backup cid={ch.id} />}
      {!can ? <p className="q">定稿后开启</p> : on ? (
        <div className="ch-bst">
          <p>{[last ? `${hm(last.at)} · ${found(last)}` : "等第一次扫描", nextScan(ing)].filter(Boolean).join(" · ")}</p>
          {got && got !== last && <p>最近新收 {md(got.at)}</p>}
          {broke && <p className="z">{md(broke.at)} 扫描出错：{broke.error}</p>}
          {mem && <p className="z">{MEMBERS[mem] || mem}{mem !== "rss" && <> · <a {...linkProps("/settings#youtube")}>设置</a></>}</p>}
        </div>
      ) : null}
      {on && <button className="ch-btn sm" disabled={!!ing.scanning} onClick={onScan}>{ing.scanning ? "扫描中…" : "立即扫描"}</button>}
      {err && <p className="ch-err">{err}</p>}
    </section>
  );
}

function Backfill({ ch, mode, b, onOpen, onCancel, onDone }: {
  ch: Channel; mode: Mode; b: BoardData; onOpen: () => void; onCancel: () => void; onDone: (text: string) => void;
}) {
  const bf = b.backfill;
  const can = mode === "locked" && ch.mode === "TRACKED" && !!ch.youtube_channel_id;
  // 贴链接: a video the listing does not show (unlisted); null while the field is closed
  const [link, setLink] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const add = async () => {
    if (!link?.trim() || busy) return;
    setBusy(true); setErr(null);
    try {
      const r = await post<{ queued: number }>(`/api/channels/${encodeURIComponent(ch.id)}/backfill/link`, { url: link.trim() });
      setLink(null);
      onDone(r.queued ? "补录排队 1 期" : "这一期已经在了");
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  return (
    <section className="ch-bp" aria-label="补录">
      <h3>补录</h3>
      {!can ? <p className="q">定稿后可补录</p> : (
        <>
          {(bf.pending > 0 || bf.running.length > 0) && (
            <div className="ch-bst">
              {bf.running.length > 1 && <p>正在补 {bf.running.length} 期</p>}
              {bf.running.map((r) => (
                <p key={r.id} className={bf.running.length > 1 ? "one" : ""}>{bf.running.length === 1 && "正在补 "}<IssueTitle title={r.title || r.id} />{r.wait_reason ? ` · ${WAIT[r.wait_reason] || r.wait_reason}` : ""}</p>
              ))}
              {bf.pending > 0 && <p>排队 {bf.pending} 期 · 视频 {hrs(bf.seconds)} 小时</p>}
              {bf.pending > 0 && <p className={bf.stop && BACKFILL_BAD.includes(bf.stop) ? "z" : ""}>{bf.stop ? BACKFILL_STOP[bf.stop] || bf.stop : paceText(bf)}</p>}
            </div>
          )}
          <div className="ch-bacts">
            <button className="ch-btn sm solid" onClick={onOpen}>按日期挑选…</button>
            {link === null && <button className="ch-btn sm" onClick={() => { setLink(""); setErr(null); }}>贴链接</button>}
            {bf.pending > 0 && <button className="ch-btn sm" onClick={onCancel}>取消排队</button>}
          </div>
          {link !== null && (
            <div className="ch-blink">
              <input autoFocus value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://youtu.be/…" aria-label="视频链接"
                inputMode="url" autoComplete="off" disabled={busy}
                onKeyDown={(e) => { if (e.key === "Enter") add(); else if (e.key === "Escape") { setLink(null); setErr(null); } }} />
              <button className="ch-btn sm solid" disabled={busy || !link.trim()} onClick={add}>{busy ? "读取中…" : "补录"}</button>
            </div>
          )}
          {err && <p className="ch-err">{err}</p>}
        </>
      )}
    </section>
  );
}

type Range = "month" | "30" | "90" | "year" | "all" | "custom";
const RANGES: { value: Range; label: string }[] = [
  { value: "month", label: "本月" }, { value: "30", label: "近 30 天" }, { value: "90", label: "近 90 天" },
  { value: "year", label: "今年" }, { value: "all", label: "全部" },
];
/** a range's first and last day (both included); 全部 has no first */
function span(r: Range): [string, string] {
  const t = new Date();
  const back = (n: number) => { const d = new Date(t); d.setDate(t.getDate() - n + 1); return ymd(d); };
  return r === "month" ? [ymd(new Date(t.getFullYear(), t.getMonth(), 1)), ymd(t)] : r === "30" ? [back(30), ymd(t)]
    : r === "90" ? [back(90), ymd(t)] : r === "year" ? [`${t.getFullYear()}-01-01`, ymd(t)] : ["", ymd(t)];
}
type Listed = { listing?: boolean; candidates: Candidate[]; listed: number; complete: boolean; library_checked?: boolean };

/** 补录 by date: a range (a preset, or two days) lists the channel's videos then, all the new ones ticked; untick what
 *  to leave out. The dates are YouTube's rough ones ("3 weeks ago"), so the range is approximate at its ends. */
function Picker({ ch, b, onClose, onDone }: { ch: Channel; b: BoardData; onClose: () => void; onDone: (text: string) => void }) {
  const [range, setRange] = useState<Range>("30");
  const [[since, until], setDays] = useState(span("30"));
  const [data, setData] = useState<Listed | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [again, setAgain] = useState(0);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const sheet = useRef<SheetHandle>(null);
  useEffect(() => {
    let alive = true;
    let t: number | undefined;
    setData(null);
    setErr(null);
    const q = new URLSearchParams();
    if (since) q.set("since", since);
    if (until) q.set("until", until);
    // a deep listing is read in the background (a minute at most): ask again until it is there
    const ask = () => get<Listed>(`/api/channels/${encodeURIComponent(ch.id)}/backfill/candidates?${q}`).then((d) => {
      if (!alive) return;
      if (d.listing) { t = window.setTimeout(ask, 1500); return; }
      setData(d);
      // Notion could not be asked: what is already in the library is unknown, so nothing is ticked for you
      // 可能已有 (a page written by hand, a05) is left for you to tick
      setPicked(new Set(d.library_checked === false ? [] : d.candidates.filter((c) => fresh(c) && !c.short && !c.maybe).map((c) => c.id)));
    }).catch((e) => { if (alive) setErr((e as Error).message); });
    ask();
    return () => { alive = false; clearTimeout(t); };
  }, [ch.id, since, until, again]);
  const pick = (r: Range) => { setRange(r); setDays(span(r)); };
  const list = data?.candidates || [];
  const open = list.filter((c) => fresh(c) && !c.short && !c.maybe);
  const chosen = list.filter((c) => picked.has(c.id));
  const all = open.length > 0 && open.every((c) => picked.has(c.id));
  const secs = chosen.reduce((a, c) => a + (c.duration || 0), 0);
  const quota = b.runway ? chosen.length * b.runway.per_issue : null;
  const toggle = (id: string) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const send = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post<{ queued: number }>(`/api/channels/${encodeURIComponent(ch.id)}/backfill`, {
        videos: chosen.map((c) => ({ id: c.id, title: c.title || "", duration: c.duration ?? null })) });
      sheet.current?.close(() => onDone(`补录排队 ${r.queued} 期 · 空闲且额度充足时逐期付印`));
    } catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  const state = (c: Candidate) => (c.here ? "已在本刊" : c.library ? "库里已有" : c.queued ? "排队中" : c.sample ? "样片" : c.short ? "太短"
    : c.maybe ? "可能已有" : "");
  return (
    <Sheet ref={sheet} title="补录" tall onClose={onClose}>
      <div className="chp ch-pk">
        <div className="ch-pkr">
          <div className="ch-pkc" role="radiogroup" aria-label="时间范围">
            {RANGES.map((o) => (
              <button key={o.value} type="button" role="radio" aria-checked={range === o.value} className={range === o.value ? "on" : ""}
                onClick={() => pick(o.value)}>{o.label}</button>
            ))}
          </div>
          <Days since={since} until={until} on={range === "custom"} onChange={(a, z) => { setRange("custom"); setDays([a, z]); }} />
        </div>
        {!data && !err ? (
          <p className="ch-pkw"><i className="spin" aria-hidden />正在列出这段时间的视频…</p>
        ) : null}
        {data && (
          <>
            <div className="ch-pkh">
              <span>{list.length ? `${list.length} 期 · 新的 ${open.length} 期` : ""}</span>
              {open.length > 0 && (
                <button className="ch-btn xs" onClick={() => setPicked(all ? new Set() : new Set(open.map((c) => c.id)))}>{all ? "全不选" : "全选"}</button>
              )}
            </div>
            <ol className="ch-pkl">
              {list.map((c) => (
                <li key={c.id} className={fresh(c) ? "" : "off"}>
                  <label>
                    <input type="checkbox" disabled={!fresh(c)} checked={picked.has(c.id)} onChange={() => toggle(c.id)} />
                    <span className="d">{age(c)}</span>
                    <span className="t"><IssueTitle title={c.title || c.id} /></span>
                    <span className="m">{c.duration ? fmt(c.duration) : ""}</span>
                    <span className={`s${(c.short || c.maybe) && fresh(c) ? " w" : ""}`} title={c.maybe && fresh(c) ? `库里「${c.maybe.title}」` : undefined}>{state(c)}</span>
                  </label>
                </li>
              ))}
              {!list.length && <li className="off"><p>这段时间没有视频</p></li>}
            </ol>
            {!data.complete && <p className="ch-pkn">只列出了频道最近 {data.listed} 期，更早的没有列进来</p>}
            {data.library_checked === false && (
              <p className="ch-err">没能核对 Notion 库里已有哪些期，所以一期都没勾 <button className="ch-btn xs" onClick={() => setAgain((n) => n + 1)}>重新核对</button></p>
            )}
          </>
        )}
        {err && <p className="ch-err">{err} <button className="ch-btn xs" onClick={() => setAgain((n) => n + 1)}>重试</button></p>}
        <footer className="ch-pkf">
          <span>{chosen.length ? <>已选 {chosen.length} 期 · 视频 {hrs(secs)} 小时 · 生产{about(secs * b.pace)}
            {quota != null && <> · {quota >= 1 ? "超过一周的额度" : <>约占周额度 <b>{pct(quota)}</b></>}</>}</> : null}</span>
          <button className="ch-btn solid" disabled={!chosen.length || busy} onClick={send}>补录{chosen.length ? ` ${chosen.length} 期` : ""}</button>
        </footer>
      </div>
    </Sheet>
  );
}

export function Board({ ch, mode, onSetIngest, flash }: {
  ch: Channel; mode: Mode; onSetIngest: (enabled: boolean, min: number) => Promise<void>; flash: (text: string) => void;
}) {
  const [fast, setFast] = useState(false);
  const [picking, setPicking] = useState(false);
  const q = usePoll<BoardData>(`/api/channels/${encodeURIComponent(ch.id)}/board`, fast ? 4000 : 30000);
  const b = q.data;
  // 立即扫描: the time of the last scan when it was pressed; a newer one (however quick) says what it found
  const asked = useRef<string | null | undefined>(undefined);
  const lastAt = b?.ingest.scans?.[0]?.at;
  const askedAt = useRef(0);
  useEffect(() => {
    const last = b?.ingest.scans?.[0];
    if (asked.current !== undefined && last && last.at !== asked.current && !b?.ingest.scanning) {
      asked.current = undefined;
      flash(last.error ? `扫描出错：${last.error}` : `扫描完 · ${found(last)}`);
    }
    if (asked.current !== undefined && Date.now() - askedAt.current > 300000) asked.current = undefined;  // it never reported
    setFast(!!b?.ingest.scanning || asked.current !== undefined);  // poll quickly only while a scan is awaited
  }, [b]); // eslint-disable-line react-hooks/exhaustive-deps
  const issues = useMemo(() => [...(b?.issues || [])].sort((x, y) => when(y) - when(x)), [b?.issues]);
  if (!b) return q.error ? <Fault error={q.error} retry={q.reload} /> : <section className="ch-board loading" aria-busy="true" />;
  const s = b.stats;
  const start = new Date(s.week_start);
  const weeks = s.weeks.map((n, i) => {
    const d = new Date(start);
    d.setDate(start.getDate() + i * 7);
    return { n, d };
  }).slice(-SPAN);
  const scan = async () => {
    asked.current = lastAt ?? null;
    askedAt.current = Date.now();
    try { await post(`/api/channels/${encodeURIComponent(ch.id)}/scan`); setFast(true); q.reload(); }
    catch (e) { asked.current = undefined; flash((e as Error).message); }
  };
  const cancel = async () => {
    try { const r = await post<{ cancelled: number }>(`/api/channels/${encodeURIComponent(ch.id)}/backfill/cancel`); flash(`已取消 ${r.cancelled} 期补录`); q.reload(); }
    catch (e) { flash((e as Error).message); }
  };
  const per = (n: number) => (s.issues ? (n / s.issues).toFixed(n / s.issues < 10 ? 1 : 0) : "—");
  return (
    <div className="ch-board">
      <div className="kpis ch-bk">
        <Kpi label="期数" value={big(s.issues)} unit="期" sub={`本月 ${s.month} · 上月 ${s.last_month}`} />
        <Kpi label="知识单元" value={big(s.units)} sub={s.issues ? `每期 ${per(s.units)}` : undefined} />
        <Kpi label="视频" value={hrs(s.seconds)} unit="小时" sub={`片段 ${big(s.clips)}`} />
        <Kpi label="校注" value={per(s.notes)} unit="条 / 期" />
        <Kpi label="入 Notion" value={big(s.delivered + s.registered)} unit="期" tone={s.failed ? "zhu" : undefined}
          sub={s.failed ? `${s.failed} 期没写进` : s.waiting ? `${s.waiting} 期等待` : undefined} />
      </div>
      <div className="ch-bmain">
        <section className="ch-bsec" aria-label="每周出刊">
          <h3>每周出刊</h3>
          {weeks.some((w) => w.n > 0) ? (
            <Columns label={`近 ${SPAN} 周每周成刊的期数`} height={96} unit="期" whole series={[{ name: "期数", cls: "s-ink" }]}
              data={weeks.map(({ n, d }) => ({
                key: d.toISOString(), label: `${d.getMonth() + 1}/${d.getDate()}`, values: [n],
                tip: <><b>{n} 期</b> {d.getMonth() + 1}月{d.getDate()}日起的一周</>,
              }))} />
          ) : <p className="q">近 {SPAN} 周还没有成刊。</p>}
        </section>
        <section className="ch-bsec" aria-label="期次">
          <h3>期次</h3>
          {issues.length ? <ol className="toc">{issues.map((it) => <Row key={it.id} it={it} own />)}</ol>
            : <p className="q">试一期、开启收录或补录，成刊后列在这里。</p>}
        </section>
      </div>
      <aside className="ch-bside">
        <Try ch={ch} flash={flash} onDone={q.reload} />
        <Intake ch={ch} mode={mode} b={b} onSet={async (en, m) => { await onSetIngest(en, m); q.reload(); }} onScan={scan} />
        <Backfill ch={ch} mode={mode} b={b} onOpen={() => setPicking(true)} onCancel={cancel} onDone={(t) => { flash(t); q.reload(); }} />
      </aside>
      {picking && <Picker ch={ch} b={b} onClose={() => setPicking(false)} onDone={(t) => { setPicking(false); flash(t); q.reload(); }} />}
    </div>
  );
}
