import { useEffect, useRef, useState } from "react";
import { post } from "./api";
import { useLeaving, usePoll } from "./hooks";
import { linkProps, navigate, usePath } from "./router";
import { AgyLoginSheet, useAgyLogin } from "./components/AgyLogin";
import { Sheet, type SheetHandle } from "./components/Sheet";
import Issues from "./pages/Issues";
import Reader from "./pages/Reader";
import ChannelPage from "./pages/Channel";
import Engine from "./pages/Engine";

function Seal() {
  return <span className="seal" aria-hidden>课</span>;
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

/** What a paste came to; `kept`: the video is in Notion already — its page, and 重印 to print it again (a06). */
type Said = { text: string; err?: boolean; kept?: { url: string; notion_url: string | null; again: boolean } };

async function submitUrl(url: string, reprint = false): Promise<Said> {
  const r = await post("/api/submit", { url: url.trim(), reprint });
  window.dispatchEvent(new Event("az:refresh"));
  if (r.items) {  // several links at once
    const had = r.items.filter((i: any) => i.existing).length;
    const kept = r.items.filter((i: any) => i.kept).length;
    const aside = [had && `${had} 期已在队列里`, kept && `${kept} 期已在刊`].filter(Boolean).join("，");
    return { text: `已排上 ${r.items.length - had - kept} 期${aside ? `（${aside}）` : ""}` };
  }
  if (r.kept) {  // REGISTERED: the page is the user's own, which a reprint would never replace
    return { text: r.kept === "REGISTERED" ? "库里已有这一期" : "这一期已在刊", kept: { url, notion_url: r.notion_url, again: r.kept === "DELIVERED" } };
  }
  return { text: r.existing ? "这一期已在队列里" : "已加入付印队列" };
}

/** A paste's answer; one already in Notion links its page and offers 重印. */
function SaidLine({ said, onSaid }: { said: Said; onSaid: (s: Said) => void }) {
  const [busy, setBusy] = useState(false);
  const k = said.kept;
  const again = async () => {
    setBusy(true);
    try { onSaid(await submitUrl(k!.url, true)); } catch (err) { onSaid({ text: String((err as Error).message || err), err: true }); }
    setBusy(false);
  };
  return (
    <>
      {said.text}
      {k?.notion_url && <a href={k.notion_url} target="_blank" rel="noreferrer">Notion ↗</a>}
      {k?.again && <button type="button" className="link" disabled={busy} onClick={again}>重印</button>}
    </>
  );
}

/** A one-line field drops the line breaks of a pasted list of links: keep them apart with spaces. */
const pasteLines = (set: (v: string) => void) => (e: React.ClipboardEvent<HTMLInputElement>) => {
  const t = e.clipboardData.getData("text");
  if (!/\n/.test(t)) return;
  e.preventDefault();
  const el = e.currentTarget;
  set((el.value.slice(0, el.selectionStart ?? el.value.length) + " " + t.trim().split(/\s+/).join(" ") + " "
    + el.value.slice(el.selectionEnd ?? el.value.length)).trim());
};

function Paste() {
  const [v, setV] = useState("");
  const [hint, setHint] = useState<Said | null>(null);
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
  const say = (s: Said) => {
    setHint(s);
    clearTimeout(hintT.current);
    hintT.current = window.setTimeout(() => setHint(null), s.kept ? 8000 : 2600);  // time to open the page or 重印
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!v.trim()) return;
    try { say(await submitUrl(v)); setV(""); }
    catch (err) { say({ text: String((err as Error).message || err), err: true }); }
  };
  return (
    <form className="paste" onSubmit={submit}>
      <input ref={ref} value={v} onChange={(e) => setV(e.target.value)} onPaste={pasteLines(setV)} placeholder="粘贴 YouTube 链接" aria-label="提交视频链接" inputMode="url" />
      <kbd>⌘K</kbd>
      {hintOut.shown && <span className={`hint${hintOut.leaving ? " leaving" : ""}`} role="status"><SaidLine said={hintOut.shown} onSaid={say} /></span>}
    </form>
  );
}

function PasteSheet({ onClose }: { onClose: () => void }) {
  const [v, setV] = useState("");
  const [msg, setMsg] = useState<Said | null>(null);
  const [busy, setBusy] = useState(false);
  const sheet = useRef<SheetHandle>(null);
  const said = (s: Said) => {  // one in Notion already stays open: its page, or 重印
    setMsg(s);
    if (!s.err && !s.kept) setTimeout(() => sheet.current?.close(), 1100);
  };
  const go = async (url: string) => {
    if (!url.trim()) return;
    setBusy(true);
    try { said(await submitUrl(url)); setV(""); }
    catch (err) { setMsg({ text: String((err as Error).message || err), err: true }); }
    setBusy(false);
  };
  const fromClipboard = async () => {
    try { const t = await navigator.clipboard.readText(); setV(t); if (/youtu/.test(t)) go(t); }
    catch { setMsg({ text: "无法读取剪贴板，请手动粘贴", err: true }); }
  };
  return (
    <Sheet ref={sheet} title="付印一期" onClose={onClose}>
      <form className="sheet-form" onSubmit={(e) => { e.preventDefault(); go(v); }}>
        <input value={v} onChange={(e) => setV(e.target.value)} onPaste={pasteLines(setV)} placeholder="https://youtu.be/…" aria-label="视频链接" inputMode="url" autoComplete="off" />
        <div className="sheet-row">
          <button type="button" className="btn" onClick={fromClipboard}>从剪贴板粘贴</button>
          <button type="submit" className="btn solid" disabled={busy || !v.trim()}>付印</button>
        </div>
      </form>
      {msg && <p className={`sheet-msg${msg.err ? " err" : ""}`} role="status"><SaidLine said={msg} onSaid={said} /></p>}
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
    submitUrl(url).then((m) => { setMsg(m.text); setTimeout(() => navigate("/", { replace: true }), 900); })
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
  const { data, reload } = usePoll<any>("/api/status", 15000);
  const [pasting, setPasting] = useState(false);
  const [signingIn, closeSignIn] = useAgyLogin();
  useChromeAutoHide();
  useEffect(() => {
    addEventListener("az:refresh", reload);
    return () => removeEventListener("az:refresh", reload);
  }, [reload]);
  useEffect(() => {
    // "付印第一期" and friends: the sheet on phones, the masthead field on desktop
    const on = () => (matchMedia("(max-width: 900px)").matches ? setPasting(true) : dispatchEvent(new Event("az:focus-paste")));
    addEventListener("az:paste", on);
    return () => removeEventListener("az:paste", on);
  }, []);
  const decisions = data?.decisions ?? 0;
  const alerts = data?.alerts?.length ?? 0;
  const tab = path.startsWith("/channels") ? "channels" : path.startsWith("/engine") ? "engine" : "issues";

  useEffect(() => {
    document.documentElement.dataset.chrome = "shown";
    if (!path.startsWith("/issue/")) document.title = tab === "channels" ? "频道 · 课代表" : tab === "engine" ? "机房 · 课代表" : "课代表";
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
        <a className="brand" {...linkProps("/")} aria-label="课代表 首页">
          <Seal />
          <span className="wordmark"><b>课代表</b></span>
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
      {signingIn && <AgyLoginSheet onClose={closeSignIn} />}
      {/* the 频道 tool keeps its state (and unsaved edits) while moving between its channels and tabs */}
      <div key={tab === "channels" ? "/channels" : path} id="main">{page}</div>
    </>
  );
}

export { navigate };
