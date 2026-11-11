/* 专题存到 Notion (topics/notion.py): where a topic goes, picked with a click (no confirm), and what became of the write —
   the answer's foot says it, the topic's head says what went wrong and the one thing to do about it. */
import { useEffect, useRef, useState } from "react";
import { get, post } from "../../api";
import { linkProps } from "../../router";
import "../../topic.css";

export type Place = { kind: "page" | "database"; id: string; title: string; icon?: string | null };
export type NotionView = {
  target: Place | null; url: string | null; state: null | "QUEUED" | "RUNNING" | "DONE" | "FAILED";
  error: { kind: "TOKEN" | "NO_ACCESS" | "GONE" | "OTHER" | "YOURS"; message: string; found?: string[] } | null;
  at: string | null; stale: boolean; token: boolean;
  /** the integration may not read the page's comments, or not answer them (design §R8-13) */
  comments: null | "NO_READ" | "NO_INSERT";
};
const NO_COMMENTS = { NO_READ: "读不到批注：integration 没开「读评论」", NO_INSERT: "回复不了批注：integration 没开「插入评论」" };

const LINK = /notion\.(so|site|com)\/|^[0-9a-f]{32}$|^[0-9a-f-]{36}$/i;
const glyph = (p: Place) => p.icon || (p.kind === "database" ? "▦" : "▤");

/** The places a topic may go, searched as you type; a pasted Notion link is checked (a channel's database is not one). */
export function PlacePicker({ current, onPick, onClose }: { current: Place | null; onPick: (p: Place | null) => void; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [list, setList] = useState<(Place & { recent?: boolean })[] | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) onClose(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    addEventListener("mousedown", close);
    addEventListener("keydown", esc);
    return () => { removeEventListener("mousedown", close); removeEventListener("keydown", esc); };
  }, [onClose]);
  useEffect(() => {
    const line = q.trim();
    let gone = false;
    setSaid(null);
    const h = setTimeout(async () => {
      try {
        if (LINK.test(line)) {
          const p = await post<Place>("/api/topics/notion/resolve", { link: line });
          if (!gone) setList([p]);
        } else {
          const r = await get<{ targets: Place[] }>(`/api/topics/notion/targets?q=${encodeURIComponent(line)}`);
          if (!gone) setList(r.targets);
        }
      } catch (e) { if (!gone) { setList([]); setSaid((e as Error).message); } }
    }, line ? 250 : 0);
    return () => { gone = true; clearTimeout(h); };
  }, [q]);
  const recent = (list || []).filter((p) => p.recent), rest = (list || []).filter((p) => !p.recent);
  const row = (p: Place) => (
    <button key={p.id} role="option" aria-selected={current?.id === p.id} className={`tp-np-i${current?.id === p.id ? " on" : ""}`} onClick={() => onPick(p)}>
      <span className="g" aria-hidden>{glyph(p)}</span><span className="nm">{p.title}</span>
    </button>
  );
  return (
    <div className="tp-np" ref={box}>
      <input className="tp-np-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索，或粘贴 Notion 链接" aria-label="存到哪里"
        autoFocus autoComplete="off" spellCheck={false} />
      <div className="tp-np-l" role="listbox" aria-label="存到哪里">
        {!q && <button role="option" aria-selected={!current} className={`tp-np-i q${!current ? " on" : ""}`} onClick={() => onPick(null)}><span className="nm">不存</span></button>}
        {said ? <p className="tp-np-say">{said}</p> : list == null ? <p className="tp-np-say">…</p> : !list.length ? <p className="tp-np-say">没有找到</p> : (
          <>
            {recent.length > 0 && <><div className="tp-np-sep" />{recent.map(row)}</>}
            {rest.length > 0 && <><div className="tp-np-sep" />{rest.map(row)}</>}
          </>
        )}
      </div>
    </div>
  );
}

/** The answer's foot: where it goes, writing, or the page to read it on */
export function NotionFoot({ n, answered, onPick, onWrite }: {
  n: NotionView; answered: boolean; onPick: (p: Place | null) => void; onWrite: () => void;
}) {
  const [open, setOpen] = useState(false);
  if (!n.token && !n.target) return null;
  const busy = n.state === "QUEUED" || n.state === "RUNNING";
  if (busy) return <div className="tp-nfoot"><span className="tp-busy">正在写进 Notion</span></div>;
  if (n.url && n.state !== "FAILED") {
    return (
      <div className="tp-nfoot">
        <a className="tp-nlink" href={n.url} target="_blank" rel="noreferrer">在 Notion 阅读 ↗</a>
        {n.comments && <span>{NO_COMMENTS[n.comments]}</span>}
        {n.stale && n.state === "DONE" && <button className="btn ghost sm" onClick={onWrite}>更新</button>}
      </div>
    );
  }
  return (
    <div className="tp-nfoot">
      <div className="tp-np-at">
        <button className="tp-nplace" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}>
          {n.target ? <>存到「{n.target.title}」</> : "存到 Notion"}
        </button>
        {open && <PlacePicker current={n.target} onClose={() => setOpen(false)} onPick={(p) => { setOpen(false); onPick(p); }} />}
      </div>
      {answered && n.target && n.state !== "FAILED" && <button className="btn ghost sm" onClick={onWrite}>写进 Notion</button>}
    </div>
  );
}

/** The topic's head, when the write did not happen: the one thing to do */
export function NotionTrouble({ n, onWrite, onPick }: { n: NotionView; onWrite: () => void; onPick: (p: Place | null) => void }) {
  const [open, setOpen] = useState(false);
  if (n.state !== "FAILED" || !n.error) return null;
  const k = n.error.kind;
  return (
    <p className="tp-ntrouble bad" role="alert">
      <span>没写进 Notion：{n.error.message}</span>
      {k === "TOKEN" ? <a className="tp-nact" {...linkProps("/settings#notion")}>去设置</a>
        : k === "GONE" ? (
          <span className="tp-np-at">
            <button className="tp-nact" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}>换地方</button>
            {open && <PlacePicker current={n.target} onClose={() => setOpen(false)} onPick={(p) => { setOpen(false); onPick(p); }} />}
          </span>
        ) : <button className="tp-nact" onClick={onWrite}>重试</button>}
    </p>
  );
}

/** Settings → 专题: where new topics go */
export function DefaultPlace({ say }: { say: (s: { text: string; bad?: boolean }) => void }) {
  const [cur, setCur] = useState<Place | null | undefined>(undefined);
  const [open, setOpen] = useState(false);
  useEffect(() => { get<{ default: Place | null }>("/api/topics/notion").then((r) => setCur(r.default)).catch(() => setCur(null)); }, []);
  const pick = async (p: Place | null) => {
    setOpen(false);
    try { setCur((await post<{ default: Place | null }>("/api/topics/notion/default", { target: p })).default); say({ text: "已保存" }); }
    catch (e) { say({ text: (e as Error).message, bad: true }); }
  };
  if (cur === undefined) return <span>…</span>;
  return (
    <span className="tp-np-at tp-np-r">
      <button className="tp-nplace" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}>{cur ? `「${cur.title}」` : "不存"}</button>
      {open && <PlacePicker current={cur} onClose={() => setOpen(false)} onPick={pick} />}
    </span>
  );
}
