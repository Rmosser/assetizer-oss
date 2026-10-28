import { useEffect, useRef, useState } from "react";
import { post } from "../api";
import { ago } from "../util";

/** The box's settings in the 机房 (knobs.py): each change is saved on its own, a moment after the last click. */
export type Save = (body: Record<string, unknown>) => Promise<void>;

export function useKnobs(reload: () => void): { save: Save; err: string | null } {
  const [err, setErr] = useState<string | null>(null);
  const save: Save = async (body) => {
    setErr(null);
    try { await post("/api/knobs", body); reload(); } catch (e) { setErr((e as Error).message); }
  };
  return { save, err };
}

/** − value +; the value shows at once, it is saved 600 ms after the last click */
export function Stepper({ k, value, lo, hi, step = 1, fmt = String, save, label }: {
  k: string; value: number; lo: number; hi: number; step?: number; fmt?: (v: number) => string; save: Save; label: string;
}) {
  const [v, setV] = useState(value);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => { if (!timer.current) setV(value); }, [value]);
  const go = (d: number) => {
    const n = Math.min(hi, Math.max(lo, Math.round((v + d * step) * 100) / 100));
    setV(n);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(async () => { await save({ [k]: n }); timer.current = undefined; }, 600);
  };
  return (
    <span className="kn-step" role="group" aria-label={label}>
      <button aria-label={`${label}减少`} disabled={v <= lo} onClick={() => go(-1)}>−</button>
      <b className="num">{fmt(v)}</b>
      <button aria-label={`${label}增加`} disabled={v >= hi} onClick={() => go(1)}>+</button>
    </span>
  );
}

const days = (v: number) => `${v} 天`;
const pct = (v: number) => `${Math.round(v * 100)}%`;

export function ParallelKnobs({ kn, save }: { kn: any; save: Save }) {
  return (
    <div className="inst">
      <span className="label">并行</span>
      <div className="kn-line"><span>同时印</span><Stepper k="worker_slots" value={kn.worker_slots} lo={1} hi={16} save={save} label="同时印" /></div>
      <div className="kn-line"><span>同时下载</span><Stepper k="download_slots" value={kn.download_slots} lo={1} hi={4} save={save} label="同时下载" /></div>
    </div>
  );
}

export function DiskKnobs({ kn, save }: { kn: any; save: Save }) {
  return (
    <>
      <div className="kn-line"><span>原片</span><Stepper k="retention_days" value={kn.retention_days} lo={1} hi={60} fmt={days} save={save} label="原片保留" /></div>
      <div className="kn-line"><span>交付后的原片</span><Stepper k="delivered_retention_days" value={kn.delivered_retention_days} lo={0} hi={30} fmt={days} save={save} label="交付后原片保留" /></div>
      <div className="kn-line"><span>低清版</span>
        <Stepper k="playback_keep_days" value={kn.playback_keep_days} lo={1} hi={60} fmt={days} save={save} label="低清版保留" />
        <Stepper k="playback_max_gb" value={kn.playback_max_gb} lo={0} hi={50} fmt={(v) => `${v} GB`} save={save} label="低清版上限" />
      </div>
    </>
  );
}

const PROVIDER: Record<string, string> = { agy: "Gemini", magpie: "ChatGPT", tuzi: "Gemini（兔子）" };

/** 分工: which provider each model stage uses (stage_providers over llm_provider), and what 补录 leaves to new videos */
export function Routing({ kn, steps, label, save }: { kn: any; steps: string[]; label: Record<string, string>; save: Save }) {
  const main = kn.llm_provider;
  const options = (cur: string) => ["agy", "magpie", ...(cur === "tuzi" ? ["tuzi"] : [])];
  const seg = (cur: string, pick: (p: string) => void, name: string) => (
    <span className="seg" role="radiogroup" aria-label={name}>
      {options(cur).map((p) => <button key={p} role="radio" aria-checked={cur === p} className={cur === p ? "on" : ""} onClick={() => cur !== p && pick(p)}>{PROVIDER[p]}</button>)}
    </span>
  );
  return (
    <details className="fold mini kn-route">
      <summary><i className="plus" aria-hidden /><span>分工与补录</span></summary>
      <div className="kn-grid">
        {steps.map((k) => (
          <div key={k} className="kn-line"><span>{label[k]}</span>
            {seg(kn.stage_providers[k] || main, (p) => save({ stage_providers: { ...kn.stage_providers, [k]: p } }), label[k])}
          </div>
        ))}
        <div className="kn-line"><span>其余</span>{seg(main, (p) => save({ llm_provider: p }), "其余")}</div>
      </div>
      <div className="kn-grid">
        <div className="kn-line"><span>补录留给新视频 · 每周</span>
          <Stepper k="backfill_min_weekly" value={kn.backfill_min_weekly} lo={0} hi={0.9} step={0.1} fmt={pct} save={save} label="补录留给新视频的周额度" /></div>
        <div className="kn-line"><span>补录留给新视频 · 5 小时</span>
          <Stepper k="backfill_min_5h" value={kn.backfill_min_5h} lo={0} hi={0.9} step={0.1} fmt={pct} save={save} label="补录留给新视频的 5 小时额度" /></div>
      </div>
    </details>
  );
}

const ASR_CODE: Record<string, string> = { UNREACHABLE: "连不上", BAD_TOKEN: "令牌不对" };

/** 转写 lamp: no address → CPU; the last test failing → what failed; else the device the last issue used */
export function asrLamp(a: any): ["ok" | "bad" | "idle", string] {
  if (!a.url) return ["idle", "CPU"];
  if (a.test && !a.test.ok) return ["bad", ASR_CODE[a.test.code] || a.test.code];
  if (a.last?.fell_back) return ["idle", "退回 CPU"];
  return ["ok", "GPU"];
}

function Secret({ id, set, value, onChange, hint, busy }: { id: string; set: boolean; value: string; onChange: (v: string) => void; hint: string; busy: boolean }) {
  return (
    <input id={id} type="password" autoComplete="off" spellCheck={false} autoCapitalize="off" autoCorrect="off" data-1p-ignore
      value={value} disabled={busy} placeholder={set ? "已设置，留空不改" : hint} onChange={(e) => onChange(e.target.value)} />
  );
}

export function AsrCard({ a, reload }: { a: any; reload: () => void }) {
  const [url, setUrl] = useState<string>(a.url || "");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setUrl(a.url || ""), [a.url]);
  const act = async (path: string, body: unknown = {}) => {
    setBusy(true); setErr(null);
    try { await post(path, body); setToken(""); reload(); } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  const dirty = url.trim().replace(/\/+$/, "") !== (a.url || "") || !!token.trim();
  const t = a.test;
  return (
    <div className="inst">
      <span className="label">GPU 转写</span>
      {a.last && <div className="sub">上一期用{a.last.device === "gpu" ? " GPU" : a.last.fell_back ? "了 CPU（GPU 没接上）" : " CPU"} · {ago(a.last.at)}</div>}
      {t && <div className={`sub${t.ok ? "" : " zhu"}`}>测试{t.ok ? `通过${t.model ? ` · ${t.model}` : ""}` : `没通过：${ASR_CODE[t.code] || t.code}`} · {ago(t.at)}</div>}
      <div className="kn-form">
        <input value={url} onChange={(e) => setUrl(e.target.value)} inputMode="url" autoComplete="off" spellCheck={false}
          placeholder="http://192.168.1.50:8790" aria-label="地址" disabled={busy} />
        <Secret id="asr-token" set={a.token} value={token} onChange={setToken} hint="令牌" busy={busy} />
      </div>
      <div className="kn-acts">
        <button className="btn sm" disabled={busy || !dirty} onClick={() => act("/api/asr", { url: url.trim() || undefined, token: token.trim() || undefined, clear: url.trim() ? [] : ["url"] })}>保存</button>
        <button className="btn sm" disabled={busy || !a.url} onClick={() => act("/api/asr/test")}>测试</button>
        {(a.url || a.token) && <button className="btn ghost sm" disabled={busy} onClick={() => act("/api/asr", { clear: ["url", "token"] })}>清除</button>}
      </div>
      {err && <p className="err-line" role="alert">{err}</p>}
    </div>
  );
}

export function TelegramCard({ tg, save, reload }: { tg: any; save: Save; reload: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const act = async (path: string, body: unknown = {}) => {
    setBusy(true); setErr(null);
    try { await post(path, body); setToken(""); reload(); } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  const on = tg.token && tg.chat;
  return (
    <div className="inst">
      <span className="label">Telegram 通知</span>
      {on ? <div className="sub">接到{tg.linked?.chat ? `「${tg.linked.chat}」` : "对话"}{tg.linked?.bot ? ` · @${tg.linked.bot}` : ""}</div>
        : tg.token ? <div className="sub">在 Telegram 里给 bot 发一句话，再点「接通」</div> : null}
      <div className="kn-form">
        <Secret id="tg-token" set={tg.token} value={token} onChange={setToken} hint="bot token（@BotFather）" busy={busy} />
      </div>
      <div className="kn-acts">
        <button className="btn sm" disabled={busy || !token.trim()} onClick={() => act("/api/telegram", { token: token.trim() })}>保存</button>
        <button className="btn sm" disabled={busy || !tg.token} onClick={() => act("/api/telegram/setup")}>{on ? "重新接通" : "接通"}</button>
        {tg.token && <button className="btn ghost sm" disabled={busy} onClick={() => act("/api/telegram", { clear: ["token"] })}>清除</button>}
      </div>
      {on && <div className="kn-line"><span>每日摘要</span><Stepper k="digest_hour" value={tg.digest_hour} lo={0} hi={23} fmt={(v) => `${v}:00`} save={save} label="每日摘要" /></div>}
      {err && <p className="err-line" role="alert">{err}</p>}
    </div>
  );
}

/** Where a video with no channel target goes: the 课代表 database under a page the user names */
export function NotionDefault({ n, reload }: { n: any; reload: () => void }) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!n.token) return null;
  const go = async () => {
    setBusy(true); setErr(null);
    try { await post("/api/notion/default", { parent: page.trim() }); setPage(""); setOpen(false); reload(); } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };
  return open ? (
    <>
      <div className="kn-form">
        <input value={page} onChange={(e) => setPage(e.target.value)} inputMode="url" autoComplete="off" spellCheck={false}
          placeholder="上级页面的链接" aria-label="上级页面" disabled={busy} autoFocus />
      </div>
      <div className="kn-acts">
        <button className="btn sm" disabled={busy || !page.trim()} onClick={go}>保存</button>
        <button className="btn ghost sm" disabled={busy} onClick={() => { setOpen(false); setErr(null); }}>取消</button>
      </div>
      {err && <p className="err-line" role="alert">{err}</p>}
    </>
  ) : <button className="btn sm inst-act" onClick={() => setOpen(true)}>{n.parent || n.database ? "换位置" : "指定位置"}</button>;
}
