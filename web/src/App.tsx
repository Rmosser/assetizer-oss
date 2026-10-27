import { useEffect, useRef, useState } from "react";
import { post } from "./api";
import { useLeaving, usePoll } from "./hooks";
import { linkProps, navigate, usePath } from "./router";
import { Sheet, type SheetHandle } from "./components/Sheet";
import Issues from "./pages/Issues";
import Reader from "./pages/Reader";
import ChannelPage from "./pages/Channel";
import Engine from "./pages/Engine";

function Seal() {
  return <span className="seal" aria-hidden>刊</span>;
}

function ThemeToggle() {
  const [dark, setDark] = useState(() =>
    document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches);
  const flip = () => {
    const t = dark ? "light" : "dark";
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("az-theme", t); } catch { /* private mode */ }
    document.querySelector('meta[name="theme-color"]:not([media])')?.setAttribute("content", t === "dark" ? "#141311" : "#f3efe6");
    setDark(!dark);
  };
  return (
    <button className="iconbtn" onClick={flip} aria-label={dark ? "切换到浅色" : "切换到深色"} title={dark ? "浅色" : "深色"}>
      {dark ? (
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><circle cx="12" cy="12" r="4.2" /><path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6" strokeLinecap="round" /></svg>
      ) : (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M20.5 14.2A8.5 8.5 0 1 1 9.8 3.5a6.8 6.8 0 0 0 10.7 10.7Z" strokeLinejoin="round" /></svg>
      )}
    </button>
  );
}

async function submitUrl(url: string): Promise<string> {
  const r = await post("/api/submit", { url: url.trim() });
  window.dispatchEvent(new Event("az:refresh"));
  return r.existing ? "这一期已在队列里" : "已加入付印队列";
}

function Paste() {
  const [v, setV] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const hintOut = useLeaving(hint);
  const hintT = useRef<number | undefined>(undefined);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); ref.current?.focus(); }
    };
    const focus = () => ref.current?.focus();
    addEventListener("keydown", on);
    addEventListener("az:focus-paste", focus);
    return () => { removeEventListener("keydown", on); removeEventListener("az:focus-paste", focus); };
  }, []);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!v.trim()) return;
    try { setHint(await submitUrl(v)); setV(""); }
    catch (err) { setHint(String((err as Error).message || err)); }
    clearTimeout(hintT.current);
    hintT.current = window.setTimeout(() => setHint(null), 2600);
  };
  return (
    <form className="paste" onSubmit={submit}>
      <input ref={ref} value={v} onChange={(e) => setV(e.target.value)} placeholder="粘贴 YouTube 链接" aria-label="提交视频链接" inputMode="url" />
      <kbd>⌘K</kbd>
      {hintOut.shown && <span className={`hint${hintOut.leaving ? " leaving" : ""}`} role="status">{hintOut.shown}</span>}
    </form>
  );
}

function PasteSheet({ onClose }: { onClose: () => void }) {
  const [v, setV] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const sheet = useRef<SheetHandle>(null);
  const go = async (url: string) => {
    if (!url.trim()) return;
    setBusy(true);
    try { setMsg(await submitUrl(url)); setV(""); setTimeout(() => sheet.current?.close(), 1100); }
    catch (err) { setMsg(String((err as Error).message || err)); }
    setBusy(false);
  };
  const fromClipboard = async () => {
    try { const t = await navigator.clipboard.readText(); setV(t); if (/youtu/.test(t)) go(t); }
    catch { setMsg("无法读取剪贴板，请手动粘贴"); }
  };
  return (
    <Sheet ref={sheet} title="付印一期" onClose={onClose}>
      <form className="sheet-form" onSubmit={(e) => { e.preventDefault(); go(v); }}>
        <input value={v} onChange={(e) => setV(e.target.value)} placeholder="https://youtu.be/…" aria-label="视频链接" inputMode="url" autoComplete="off" />
        <div className="sheet-row">
          <button type="button" className="btn" onClick={fromClipboard}>从剪贴板粘贴</button>
          <button type="submit" className="btn solid" disabled={busy || !v.trim()}>付印</button>
        </div>
      </form>
      {msg && <p className="sheet-msg" role="status">{msg}</p>}
    </Sheet>
  );
}

/** Android share target: /share?url=… or ?text=… → submit, then go home. */
function Share() {
  const [msg, setMsg] = useState("正在收下这个链接…");
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    const raw = q.get("url") || q.get("text") || "";
    const url = raw.match(/https?:\/\/\S+/)?.[0] || raw;
    if (!url) { navigate("/", { replace: true }); return; }
    submitUrl(url).then((m) => { setMsg(m); setTimeout(() => navigate("/", { replace: true }), 900); })
      .catch((e) => setMsg(String((e as Error).message || e)));
  }, []);
  return <main className="page"><div className="state-block"><h3>{msg}</h3></div></main>;
}

/** Phones: the masthead slides away while reading down and returns on the way up. */
function useChromeAutoHide() {
  useEffect(() => {
    const root = document.documentElement;
    const mq = matchMedia("(max-width: 900px)");
    let last = scrollY, raf = 0;
    const on = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const d = scrollY - last;
        if (!mq.matches || scrollY < 120) root.dataset.chrome = "shown";
        else if (Math.abs(d) > 8) root.dataset.chrome = d > 0 ? "hidden" : "shown";
        if (Math.abs(d) > 8 || scrollY < 120) last = scrollY;
      });
    };
    addEventListener("scroll", on, { passive: true });
    return () => { removeEventListener("scroll", on); cancelAnimationFrame(raf); };
  }, []);
}

export default function App() {
  const path = usePath();
  const { data } = usePoll<any>("/api/overview", 15000);
  const [pasting, setPasting] = useState(false);
  useChromeAutoHide();
  useEffect(() => {
    // "付印第一期" and friends: the sheet on phones, the masthead field on desktop
    const on = () => (matchMedia("(max-width: 900px)").matches ? setPasting(true) : dispatchEvent(new Event("az:focus-paste")));
    addEventListener("az:paste", on);
    return () => removeEventListener("az:paste", on);
  }, []);
  const decisions = data?.decisions?.length ?? 0;
  const alerts = data?.alerts?.length ?? 0;
  const tab = path.startsWith("/channels") ? "channels" : path.startsWith("/engine") ? "engine" : "issues";

  useEffect(() => {
    document.documentElement.dataset.chrome = "shown";
    if (!path.startsWith("/issue/")) document.title = tab === "channels" ? "频道 · 资产刊" : tab === "engine" ? "机房 · 资产刊" : "资产刊";
  }, [path, tab]);

  let page;
  const m = path.match(/^\/issue\/([^/]+)/);
  if (m) page = <Reader id={decodeURIComponent(m[1])} />;
  else if (path === "/share") page = <Share />;
  else if (tab === "channels") page = <ChannelPage />;
  else if (tab === "engine") page = <Engine />;
  else page = <Issues />;

  return (
    <>
      <a className="skip" href="#main">跳到正文</a>
      <header className="masthead">
        <a className="brand" {...linkProps("/")} aria-label="资产刊 首页">
          <Seal />
          <span className="wordmark"><b>资产刊</b></span>
        </a>
        <nav className="nav" aria-label="主导航">
          <a {...linkProps("/")} className={tab === "issues" ? "on" : ""} aria-current={tab === "issues" ? "page" : undefined}>
            刊
          </a>
          <a {...linkProps("/channels")} className={tab === "channels" ? "on" : ""} aria-current={tab === "channels" ? "page" : undefined}>
            频道{decisions > 0 && <sup className="count" aria-label={`${decisions} 件待你拍板`}>{decisions}</sup>}
          </a>
          <a {...linkProps("/engine")} className={tab === "engine" ? "on" : ""} aria-current={tab === "engine" ? "page" : undefined}>
            机房{alerts > 0 && <sup className="count" aria-label={`${alerts} 条告警`}>!</sup>}
          </a>
        </nav>
        <div className="tools">
          <div className="paste-wrap"><Paste /></div>
          <button className="iconbtn paste-btn" onClick={() => setPasting(true)} aria-label="付印一期">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
          </button>
          <ThemeToggle />
        </div>
      </header>
      {pasting && <PasteSheet onClose={() => setPasting(false)} />}
      {/* the 频道 tool keeps its state (and unsaved edits) while moving between its channels and tabs */}
      <div key={tab === "channels" ? "/channels" : path} id="main">{page}</div>
    </>
  );
}

export { navigate };
