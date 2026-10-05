import { useEffect, useRef, useState } from "react";
import { post } from "./api";
import { usePoll } from "./hooks";
import { linkProps, navigate, usePath } from "./router";
import Issues from "./pages/Issues";
import Reader from "./pages/Reader";
import Channels from "./pages/Channels";
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

function Paste({ autoFocus = false, onDone }: { autoFocus?: boolean; onDone?: () => void } = {}) {
  const [v, setV] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); ref.current?.focus(); }
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, []);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!v.trim()) return;
    try {
      const r = await post("/api/submit", { url: v.trim() });
      setHint(r.existing ? "这一期已在队列里" : "已加入付印队列");
      setV("");
      onDone?.();
      window.dispatchEvent(new Event("az:refresh"));
    } catch (err) {
      setHint(String((err as Error).message || err));
    }
    setTimeout(() => setHint(null), 2600);
  };
  return (
    <form className="paste" onSubmit={submit}>
      <input ref={ref} autoFocus={autoFocus} value={v} onChange={(e) => setV(e.target.value)} placeholder="粘贴 YouTube 链接，回车付印" aria-label="提交视频链接" />
      <kbd>⌘K</kbd>
      {hint && <span className="hint" role="status">{hint}</span>}
    </form>
  );
}

function PasteSheet() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className="iconbtn only-mobile" onClick={() => setOpen(true)} aria-label="提交视频链接">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
      </button>
      {open && (
        <div className="sheet-backdrop" onClick={() => setOpen(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="付印一期">
            <div className="sheet-grip" />
            <h3>付印一期</h3>
            <p className="muted">粘贴 YouTube 链接，系统会自动理解、拆解、剪辑并写好文章。</p>
            <Paste autoFocus onDone={() => setTimeout(() => setOpen(false), 1200)} />
          </div>
        </div>
      )}
    </>
  );
}

const TABS = [
  { key: "issues", to: "/", label: "刊", icon: <path d="M5 4h10l4 4v12H5z M15 4v4h4 M8 12h8 M8 16h6" /> },
  { key: "channels", to: "/channels", label: "频道", icon: <path d="M4 7h16M4 12h16M4 17h10" /> },
  { key: "engine", to: "/engine", label: "机房", icon: <path d="M4 6h16v5H4z M4 13h16v5H4z M7 8.5h.01 M7 15.5h.01" /> },
];

export default function App() {
  const path = usePath();
  const { data } = usePoll<any>("/api/overview", 15000);
  const flags = data?.open_flags ?? 0;
  const alerts = data?.alerts?.length ?? 0;
  const tab = path.startsWith("/channels") ? "channels" : path.startsWith("/engine") ? "engine" : "issues";

  useEffect(() => {
    document.title = tab === "channels" ? "频道 · 资产刊" : tab === "engine" ? "机房 · 资产刊" : "资产刊";
  }, [tab]);

  let page;
  const m = path.match(/^\/issue\/([^/]+)/);
  if (m) page = <Reader id={decodeURIComponent(m[1])} />;
  else if (tab === "channels") page = <Channels />;
  else if (tab === "engine") page = <Engine />;
  else page = <Issues />;

  return (
    <>
      <header className="masthead">
        <a className="brand" {...linkProps("/")} aria-label="资产刊 首页">
          <Seal />
          <span className="wordmark"><b>资产刊</b><span>ASSETIZER</span></span>
        </a>
        <nav className="nav" aria-label="主导航">
          <a {...linkProps("/")} className={tab === "issues" ? "on" : ""}>刊{flags > 0 && <span className="count">{flags}</span>}</a>
          <a {...linkProps("/channels")} className={tab === "channels" ? "on" : ""}>频道</a>
          <a {...linkProps("/engine")} className={tab === "engine" ? "on" : ""}>机房{alerts > 0 && <span className="count">!</span>}</a>
        </nav>
        <div className="tools">
          <div className="only-desktop paste-wrap"><Paste /></div>
          <PasteSheet />
          <ThemeToggle />
        </div>
      </header>
      <div key={path}>{page}</div>
      <nav className="tabbar" aria-label="主导航（移动端）">
        {TABS.map((t) => (
          <a key={t.key} {...linkProps(t.to)} className={tab === t.key ? "on" : ""}>
            <span className="tab-ico">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round">{t.icon}</svg>
              {t.key === "issues" && flags > 0 && <span className="count">{flags}</span>}
              {t.key === "engine" && alerts > 0 && <span className="count">!</span>}
            </span>
            <span className="tab-label">{t.label}</span>
          </a>
        ))}
      </nav>
    </>
  );
}

export { navigate };
