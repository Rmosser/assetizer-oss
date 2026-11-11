import { useCallback, useEffect, useRef, useState } from "react";
import { get, HttpError, post } from "../api";
import { asrCode } from "../box";
import { usePoll } from "../hooks";
import { ago, at8 } from "../util";
import { Cmd } from "./Cmd";

/** The box's settings (knobs.py). A control saves on its own, a moment after the last click; true: it was saved. A form with
 *  text in it saves when asked to, so half an address is never stored. Either way the page says how it went. */
export type Save = (body: Record<string, unknown>) => Promise<boolean>;
export type Said = { text: string; bad?: boolean };

export function useKnobs(reload: () => void): { save: Save; said: Said | null; say: (s: Said) => void } {
  const [said, setSaid] = useState<Said | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const say = (s: Said) => {
    setSaid(s);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setSaid(null), s.bad ? 8000 : 1800);
  };
  const save: Save = async (body) => {
    try { await post("/api/knobs", body); reload(); say({ text: "已保存" }); return true; }
    catch (e) { say({ text: (e as Error).message, bad: true }); return false; }
  };
  return { save, said, say };
}

/** A line of the 设置 page: what, then its control on the right */
export function Row({ name, note, children, className = "" }: { name: React.ReactNode; note?: React.ReactNode; children?: React.ReactNode; className?: string }) {
  return (
    <div className={`st-row ${className}`}>
      <div className="st-k">{name}{note && <span className="st-note">{note}</span>}</div>
      {children != null && <div className="st-v">{children}</div>}
    </div>
  );
}

/** A value changed here and saved `wait` ms after the last change. It shows at once; once nothing waits to be saved, the
 *  page shows what is stored again (what was saved, read back; what was there, if saving failed). Leaving the page sends
 *  what is still waiting; `drop` forgets it and shows `to`, or what is stored. */
export function useLater<T>(stored: T, send: (v: T) => Promise<boolean>, wait: number) {
  const [v, setV] = useState(stored);
  const timer = useRef<number | undefined>(undefined);
  const waiting = useRef<{ v: T } | null>(null);
  const latest = useRef({ stored, send });
  latest.current = { stored, send };
  useEffect(() => { if (!timer.current) setV(stored); }, [stored]);
  useEffect(() => () => {
    clearTimeout(timer.current);
    if (waiting.current) void latest.current.send(waiting.current.v);
  }, []);
  const set = (n: T) => {
    setV(n);
    waiting.current = { v: n };
    clearTimeout(timer.current);
    const mine = window.setTimeout(async () => {
      waiting.current = null;
      const ok = await latest.current.send(n);
      if (timer.current !== mine) return;  // changed again while it was being saved: that one has the last word
      timer.current = undefined;
      if (!ok) setV(latest.current.stored);
    }, wait);
    timer.current = mine;
  };
  const drop = useCallback((to?: T) => {
    clearTimeout(timer.current);
    timer.current = undefined;
    waiting.current = null;
    setV(to === undefined ? latest.current.stored : to);
  }, []);
  return [v, set, drop] as const;
}

/** − value +; the value shows at once, it is saved 600 ms after the last click, and goes back to what is stored if that fails.
 *  At the end of its range a button stays focusable, so the keyboard is not thrown out of the row.
 *  `body`: what to send for a value, where it is not simply {k: value}; `drop`: set to forget a value not sent yet */
export function Stepper({ k, value, lo, hi, step = 1, fmt = String, save, label, body, drop }: {
  k?: string; value: number; lo: number; hi: number; step?: number; fmt?: (v: number) => string; save: Save; label: string;
  body?: (v: number) => Record<string, unknown>; drop?: React.MutableRefObject<((to?: number) => void) | null>;
}) {
  const [v, set, forget] = useLater(value, (n: number) => save(body ? body(n) : { [k as string]: n }), 600);
  useEffect(() => {
    if (!drop) return;
    drop.current = forget;
    return () => { drop.current = null; };
  }, [drop, forget]);
  const go = (d: number) => {
    const n = Math.min(hi, Math.max(lo, Math.round((v + d * step) * 100) / 100));
    if (n !== v) set(n);
  };
  return (
    <span className="kn-step" role="group" aria-label={label}>
      <button type="button" aria-label={`${label}减少`} aria-disabled={v <= lo} onClick={() => go(-1)}>−</button>
      <b className="num" aria-live="polite">{fmt(v)}</b>
      <button type="button" aria-label={`${label}增加`} aria-disabled={v >= hi} onClick={() => go(1)}>+</button>
    </span>
  );
}

const days = (v: number) => `${v} 天`;

export function ParallelRows({ kn, save }: { kn: any; save: Save }) {
  return (
    <>
      <Row name="同时印"><Stepper k="worker_slots" value={kn.worker_slots} lo={1} hi={16} fmt={(v) => `${v} 期`} save={save} label="同时印" /></Row>
      <Row name="同时下载"><Stepper k="download_slots" value={kn.download_slots} lo={1} hi={4} fmt={(v) => `${v} 个`} save={save} label="同时下载" /></Row>
    </>
  );
}

export function DiskRows({ kn, save }: { kn: any; save: Save }) {
  return (
    <>
      <Row name="原片保留"><Stepper k="retention_days" value={kn.retention_days} lo={1} hi={60} fmt={days} save={save} label="原片保留" /></Row>
      <Row name="交付后原片保留"><Stepper k="delivered_retention_days" value={kn.delivered_retention_days} lo={0} hi={30} fmt={days} save={save} label="交付后原片保留" /></Row>
      <Row name="低清版保留"><Stepper k="playback_keep_days" value={kn.playback_keep_days} lo={1} hi={60} fmt={days} save={save} label="低清版保留" /></Row>
      <Row name="低清版上限"><Stepper k="playback_max_gb" value={kn.playback_max_gb} lo={0} hi={50} fmt={(v) => `${v} GB`} save={save} label="低清版上限" /></Row>
    </>
  );
}

export function Secret({ set, value, onChange, hint, busy, label }: { set: boolean; value: string; onChange: (v: string) => void; hint?: string; busy: boolean; label: string }) {
  return (
    <input className="st-input" type="password" autoComplete="off" spellCheck={false} autoCapitalize="off" autoCorrect="off" data-1p-ignore
      aria-label={label} value={value} disabled={busy} placeholder={set ? "已设置，留空不改" : hint} onChange={(e) => onChange(e.target.value)} />
  );
}

/** 清除 takes a second click: what it clears has to be fetched again from where it was issued (断开 too).
 *  `solid`: the sheet's own button (移到回收站), at full size */
export function Clear({ disabled, onClear, label = "清除", sure = "确认清除", solid = false }: {
  disabled: boolean; onClear: () => void; label?: string; sure?: string; solid?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button type="button" className={`btn ${solid ? "solid" : "sm"}${armed ? " armed" : solid ? "" : " ghost"}`} disabled={disabled} onBlur={() => setArmed(false)}
      onClick={() => { if (armed) { setArmed(false); onClear(); } else setArmed(true); }}>{armed ? sure : label}</button>
  );
}

/** A form of the page: posts, and says how it went. */
function useAct(reload: () => void, say: (s: Said) => void, done: () => void) {
  const [busy, setBusy] = useState(false);
  const act = async (path: string, body: unknown = {}, ok?: string) => {
    setBusy(true);
    try { await post(path, body); done(); reload(); if (ok) say({ text: ok }); }
    catch (e) { say({ text: (e as Error).message, bad: true }); }
    setBusy(false);
  };
  return { busy, act };
}

export function AsrForm({ a, reload, say }: { a: any; reload: () => void; say: (s: Said) => void }) {
  const [url, setUrl] = useState<string>(a.url || "");
  const [token, setToken] = useState("");
  const { busy, act } = useAct(reload, say, () => setToken(""));
  useEffect(() => setUrl(a.url || ""), [a.url]);
  const dirty = url.trim().replace(/\/+$/, "") !== (a.url || "") || !!token.trim();
  const t = a.test;
  const submit = () => act("/api/asr", { url: url.trim() || undefined, token: token.trim() || undefined, clear: url.trim() ? [] : ["url"] }, "已保存");
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (dirty && !busy) submit(); }}>
      {a.last && <Row name="上一期">{a.last.device === "gpu" ? "GPU" : a.last.fell_back ? "CPU（GPU 没接上）" : "CPU"} · {ago(a.last.at)}</Row>}
      {t && <Row name="测试"><span className={t.ok ? undefined : "zhu"}>{t.ok ? `通过${t.model ? ` · ${t.model}` : ""}` : `没通过：${asrCode(t.code)}`} · {ago(t.at)}</span></Row>}
      <Row name="地址">
        <input className="st-input mono" value={url} onChange={(e) => setUrl(e.target.value)} inputMode="url" autoComplete="off" spellCheck={false}
          placeholder="http://192.168.1.50:8790" aria-label="地址" disabled={busy} />
      </Row>
      <Row name="令牌"><Secret set={a.token} value={token} onChange={setToken} busy={busy} label="令牌" /></Row>
      {(dirty || a.url || a.token) && (
        <div className="st-acts">
          {!dirty && (a.url || a.token) && <Clear disabled={busy} onClear={() => act("/api/asr", { clear: ["url", "token"] }, "已清除")} />}
          {!dirty && a.url && <button type="button" className="btn sm" disabled={busy} onClick={() => act("/api/asr/test")}>测试</button>}
          {dirty && <button type="submit" className="btn sm solid" disabled={busy}>保存</button>}
        </div>
      )}
    </form>
  );
}

export function TelegramForm({ tg, save, reload, say }: { tg: any; save: Save; reload: () => void; say: (s: Said) => void }) {
  const [token, setToken] = useState("");
  const { busy, act } = useAct(reload, say, () => setToken(""));
  const on = tg.token && tg.chat;
  const typed = !!token.trim();
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (typed && !busy) act("/api/telegram", { token: token.trim() }, "已保存"); }}>
      {on && <Row name="接到">{tg.linked?.chat ? `「${tg.linked.chat}」` : "对话"}{tg.linked?.bot ? ` · @${tg.linked.bot}` : ""}</Row>}
      {on && <Row name="每日摘要"><Stepper k="digest_hour" value={tg.digest_hour} lo={0} hi={23} fmt={(v) => `${v}:00`} save={save} label="每日摘要" /></Row>}
      <Row name="Bot token"><Secret set={tg.token} value={token} onChange={setToken} hint="@BotFather 给的 token" busy={busy} label="Bot token" /></Row>
      {(typed || tg.token) && (
        <div className="st-acts">
          {!typed && !on && <span className="st-hint">在 Telegram 里给 bot 发一句话，再点「接通」</span>}
          {!typed && <Clear disabled={busy} onClear={() => act("/api/telegram", { clear: ["token"] }, "已清除")} />}
          {!typed && <button type="button" className={`btn sm${on ? "" : " solid"}`} disabled={busy} onClick={() => act("/api/telegram/setup")}>{on ? "重新接通" : "接通"}</button>}
          {typed && <button type="submit" className="btn sm solid" disabled={busy}>保存</button>}
        </div>
      )}
    </form>
  );
}

/** Where a video with no channel target goes: the 课代表 database under a page the user names */
export function NotionRows({ n, reload, say }: { n: any; reload: () => void; say: (s: Said) => void }) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState("");
  const { busy, act } = useAct(reload, say, () => { setPage(""); setOpen(false); });
  if (!n.token) return <Row name="Notion token">没有放</Row>;
  const where = n.database ? n.title : n.parent ? "第一次交付时新建" : "未指定";
  return (
    <>
      <Row name="默认库">
        <span>{where}</span>
        {!open && <button type="button" className="btn sm" onClick={() => setOpen(true)}>{n.parent || n.database ? "换位置" : "指定位置"}</button>}
      </Row>
      {open && (
        <form onSubmit={(e) => { e.preventDefault(); if (page.trim() && !busy) act("/api/notion/default", { parent: page.trim() }, "已保存"); }}>
          <Row name="上级页面">
            <input className="st-input mono" value={page} onChange={(e) => setPage(e.target.value)} inputMode="url" autoComplete="off" spellCheck={false}
              placeholder="页面链接" aria-label="上级页面" disabled={busy} autoFocus onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }} />
          </Row>
          <div className="st-acts">
            <button type="button" className="btn ghost sm" disabled={busy} onClick={() => setOpen(false)}>取消</button>
            <button type="submit" className={`btn sm${page.trim() ? " solid" : ""}`} disabled={busy || !page.trim()}>保存</button>
          </div>
        </form>
      )}
      {n.asks && <Row name="批注">近 30 天点名课代表 {n.asks.n} 次{n.asks.todo ? `，待修 ${n.asks.todo} 条` : ""}</Row>}
    </>
  );
}

type Hook = {
  url: string | null; token: "none" | "set"; token_at: string | null; rearm_until: string | null; tunnel: "off" | "up" | "down";
  events: { total: number; rejected: number; rejected_at: string | null; last_at: string | null; last_type: string | null; signed_at: string | null };
};
const HOOK = "/api/notion/hook";

/** where the hook stands, the first thing in the way first */
function hookLine(h: Hook): { lamp: string; text: string } {
  if (h.tunnel === "off") return { lamp: "idle", text: "隧道没开" };
  if (h.tunnel === "down") return { lamp: "bad", text: "隧道连不上" };
  if (h.token === "none") return { lamp: "idle", text: "等 Notion 发验证令牌" };
  if (!h.events.signed_at) return { lamp: "warn", text: "令牌已到 · 验证后写一条批注试试" };  // Verify sends nothing: the first comment does
  return { lamp: "ok", text: h.events.last_at ? `已通 · ${ago(h.events.last_at)}` : "已通" };
}

/** Notion 批注推送: Notion posts new comments to the box through the tunnel. Its address goes into the integration's
 *  Webhooks; the verification token Notion then sends goes back into its Verify, copied from here and never shown.
 *  Read every 10 s while something is on its way (a token, the first signed event, a 重新接收 window open), else once. */
export function NotionHook({ save, say }: { save: Save; say: (s: Said) => void }) {
  const [every, setEvery] = useState(0);
  const { data: h, error, reload, setData } = usePoll<Hook>(HOOK, every);
  const [url, setUrl] = useState<string | null>(null);  // the address being typed; null: not being changed
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const open = h?.rearm_until != null;  // the box says null once the window is over
  const waiting = !!h && (open || (h.tunnel !== "off" && (h.token === "none" || !h.events.signed_at)));
  useEffect(() => setEvery(waiting ? 10000 : 0), [waiting]);
  useEffect(() => {
    addEventListener("az:refresh", reload);
    return () => removeEventListener("az:refresh", reload);
  }, [reload]);
  const fail = (text: string) => say({ text, bad: true });
  const said = (e: unknown, other: string) => e instanceof HttpError || (e as Error)?.name === "AuthExpired" ? (e as Error).message : other;

  if (!h) {
    return (
      <>
        <h3 className="st-group">批注推送</h3>
        <Row name="状态">{error ? <span className="zhu" title={error.message}>读不到</span> : "…"}</Row>
      </>
    );
  }

  const copyToken = async () => {
    // asked for only now and handed straight to the clipboard: not kept, not shown, not logged
    const token = get<{ token: string }>(`${HOOK}/token`).then((r) => r.token);
    try {
      try {
        // Safari lets a page write the clipboard only within the click: give it the answer still on its way
        const blob = token.then((t) => new Blob([t], { type: "text/plain" }));
        blob.catch(() => {});
        await navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
      } catch {
        await navigator.clipboard.writeText(await token);
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (e) {
      fail(e instanceof HttpError && e.status === 404 ? "还没收到令牌" : said(e, "复制失败"));
    }
  };
  const rearm = async () => {
    setBusy(true);
    try {
      const r = await post<{ rearm_until: string }>(`${HOOK}/rearm`, {});
      setData((d) => (d ? { ...d, rearm_until: r.rearm_until } : d));
      reload();
    } catch (e) { fail(said(e, "没开成")); }
    setBusy(false);
  };
  const editing = url !== null || !h.url;
  const typed = (url ?? "").trim().replace(/\/+$/, "");
  const dirty = !!typed && typed !== h.url;
  const saveUrl = async () => {
    if (!dirty || busy) return;
    setBusy(true);
    if (await save({ notion_hook_url: typed })) {
      setData((d) => (d ? { ...d, url: typed } : d));
      setUrl(null);
      reload();
    }
    setBusy(false);
  };

  const line = hookLine(h);
  const ra = h.events.rejected_at;
  const rejected = h.events.rejected > 0 && !!ra && Date.now() - Date.parse(ra) < 24 * 3600e3;  // a day on, an old story
  const note = [rejected && `拒收 ${h.events.rejected} 次 · ${ago(ra)}`, error && "刷新失败"].filter(Boolean).join(" · ");
  return (
    <form onSubmit={(e) => { e.preventDefault(); void saveUrl(); }}>
      <h3 className="st-group">批注推送</h3>
      <Row name="状态" note={note || undefined}>
        <span className={`st-state ${line.lamp}`}><i className={`lamp ${line.lamp}`} aria-hidden />{line.text}</span>
      </Row>
      <Row name="地址">
        {editing
          ? <input className="st-input mono" value={url ?? ""} onChange={(e) => setUrl(e.target.value)} inputMode="url" autoComplete="off" spellCheck={false}
              placeholder="https://…/api/notion/webhook" aria-label="地址" disabled={busy} autoFocus={!!h.url}
              onKeyDown={(e) => { if (e.key === "Escape" && h.url) setUrl(null); }} />
          : <Cmd text={h.url!} />}
      </Row>
      {(h.url || dirty || open || h.token === "set") && <div className="st-acts">
        {editing && h.url && <button type="button" className="btn ghost sm" disabled={busy} onClick={() => setUrl(null)}>取消</button>}
        {editing && (dirty || h.url) && <button type="submit" className={`btn sm${dirty ? " solid" : ""}`} disabled={busy || !dirty}>保存</button>}
        {!editing && <button type="button" className="btn ghost sm" onClick={() => setUrl(h.url)}>改地址</button>}
        {open
          ? <span className="st-until num">到 {at8(h.rearm_until!)} 前接收新令牌</span>
          : h.token === "set" && <button type="button" className="btn sm" disabled={busy} onClick={rearm}>重新接收</button>}
        {h.token === "set" && (
          <button type="button" className={`btn sm${h.events.signed_at ? "" : " solid"}`} onClick={copyToken}>{copied ? "已复制" : "复制验证令牌"}</button>
        )}
      </div>}
    </form>
  );
}
