import { useEffect, useRef, useState } from "react";
import { get, post } from "../api";
import { Cmd } from "./Cmd";
import { Sheet, type SheetHandle } from "./Sheet";

/* 登录 agy from the page: the server runs agy's own sign-in and hands over its Google link; the code Google shows at the
   end comes back here. Closing the sheet leaves the sign-in waiting (reopening rejoins it) until its link expires. */

type Bucket = { window: string; remaining: number | null; disabled: boolean };
type Login = {
  state: "idle" | "checking" | "starting" | "url" | "verifying" | "done" | "already" | "failed" | "expired" | "cancelled";
  url?: string; expires_in?: number; error?: string | null;
  quota?: { groups?: { name: string; buckets: Bucket[] }[]; production_group?: string };
};

const CLI = "lzc-cli docker -- exec -it cloudlazycatappassetizer-worker-1 /lzcapp/var/assetizer/home/.local/bin/agy";
const BUSY = ["checking", "starting", "verifying"];
const ENDED = ["failed", "expired", "cancelled", "idle"];  // idle: the server no longer has it (the web process restarted)

function weekLeft(q: Login["quota"]) {
  const g = q?.groups?.find((x) => x.name === q.production_group);
  const b = g?.buckets.find((x) => x.window === "weekly" && !x.disabled);
  return b?.remaining == null ? null : Math.round(b.remaining * 100);
}

export function AgyLoginSheet({ onClose }: { onClose: () => void }) {
  const [s, setS] = useState<Login>({ state: "checking" });
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [left, setLeft] = useState<number | null>(null);
  const [lost, setLost] = useState(false);
  const misses = useRef(0);
  const sheet = useRef<SheetHandle>(null);
  const field = useRef<HTMLInputElement>(null);

  const start = async () => {
    setErr(null);
    setLost(false);
    misses.current = 0;
    setCode("");
    try { setS(await post<Login>("/api/agy/login")); } catch (e) { setErr((e as Error).message); }
  };
  useEffect(() => { start(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {  // follow the server: fast while it works, slowly while it waits for the code
    if (!BUSY.includes(s.state) && s.state !== "url") return;
    const t = setTimeout(async () => {
      try {
        const next = await get<Login>("/api/agy/login");
        misses.current = 0;
        setLost(false);
        setS(next);
      } catch {  // keep the state and ask again (a new object runs this effect once more); say so if it goes on
        misses.current += 1;
        if (misses.current >= 3) setLost(true);
        setS((x) => ({ ...x }));
      }
    }, s.state === "url" ? 5000 : 1000);
    return () => clearTimeout(t);
  }, [s]);

  useEffect(() => {  // the link's remaining time, counted down here between polls
    if (s.state !== "url" || s.expires_in == null) { setLeft(null); return; }
    const end = Date.now() + s.expires_in * 1000;
    const tick = () => setLeft(Math.max(0, Math.round((end - Date.now()) / 1000)));
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, [s.state, s.expires_in]);

  useEffect(() => {
    if (s.state === "url") field.current?.focus({ preventScroll: true });
    if (s.state === "done") dispatchEvent(new Event("az:refresh"));
  }, [s.state]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    try { setS(await post<Login>("/api/agy/login/code", { code })); setCode(""); } catch (x) { setErr((x as Error).message); }
  };
  const paste = async () => {
    try { setCode((await navigator.clipboard.readText()).trim()); } catch { setErr("读不到剪贴板，手动粘贴"); }
  };
  const copy = async () => {
    try { await navigator.clipboard.writeText(s.url || ""); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* ignore */ }
  };
  const week = weekLeft(s.quota);
  const mmss = left == null ? "" : `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;

  return (
    <Sheet ref={sheet} title="登录 agy" onClose={onClose}>
      <div className="agy" aria-live="polite">
        {BUSY.includes(s.state) && (
          <p className="agy-wait"><i className="spin" aria-hidden />{s.state === "verifying" ? "正在登录…" : "正在打开 agy 的登录…"}</p>
        )}
        {s.state === "url" && s.url && (
          <ol className="agy-steps">
            <li>
              <span className="agy-n" aria-hidden>1</span>
              <div className="agy-row">
                <a className="btn solid" href={s.url} target="_blank" rel="noreferrer noopener">打开 Google 授权<span aria-hidden> ↗</span></a>
                <button type="button" className="btn ghost sm" onClick={copy}>{copied ? "已复制" : "复制链接"}</button>
              </div>
            </li>
            <li>
              <span className="agy-n" aria-hidden>2</span>
              <form className="agy-row" onSubmit={submit}>
                <input ref={field} value={code} onChange={(e) => setCode(e.target.value)} placeholder="授权码" aria-label="授权码"
                  autoComplete="off" autoCapitalize="off" autoCorrect="off" spellCheck={false} data-1p-ignore />
                <button type="button" className="btn ghost sm" onClick={paste}>粘贴</button>
                <button type="submit" className="btn solid" disabled={!code.trim()}>登录</button>
              </form>
            </li>
            {mmss && <li className="agy-ttl">{mmss} 后链接失效</li>}
          </ol>
        )}
        {(s.state === "done" || s.state === "already") && (
          <div className="agy-ok" role="status">
            <b>{s.state === "done" ? "已登录" : "agy 已登录"}</b>
            {week != null && <span>Gemini 周额度剩 {week}%</span>}
            <button type="button" className="btn solid" onClick={() => sheet.current?.close()}>完成</button>
          </div>
        )}
        {ENDED.includes(s.state) && (
          <div className="agy-bad" role="alert">
            <p>{s.error || (s.state === "idle" ? "登录已中断" : "登录已取消")}</p>
            <button type="button" className="btn solid" onClick={start}>重新获取链接</button>
            <details className="fold mini">
              <summary><i className="plus" aria-hidden /><span>在终端里登录</span></summary>
              <Cmd text={CLI} />
            </details>
          </div>
        )}
        {err && <p className="agy-err" role="alert">{err}</p>}
        {lost && <p className="agy-err" role="status">连不上资产刊，正在重试…</p>}
      </div>
    </Sheet>
  );
}

/** App-level: the sheet opens on `az:agy-login`, and by itself after a reload while a sign-in still waits for its code. */
export function useAgyLogin(): [boolean, () => void] {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const on = () => setOpen(true);
    addEventListener("az:agy-login", on);
    get<Login>("/api/agy/login").then((s) => { if (s.state === "url" || s.state === "verifying") setOpen(true); }).catch(() => {});
    return () => removeEventListener("az:agy-login", on);
  }, []);
  return [open, () => setOpen(false)];
}

export const openAgyLogin = () => dispatchEvent(new Event("az:agy-login"));

/** The sign-in alert is about agy (not a model key gone bad): only then does 登录 agy fix it. */
export function agyAlert(a: { key: string; action?: string | null }) {
  if (a.key !== "AGY_AUTH_REQUIRED") return false;
  try { return (JSON.parse(a.action || "{}").provider || "agy") === "agy"; } catch { return true; }
}
