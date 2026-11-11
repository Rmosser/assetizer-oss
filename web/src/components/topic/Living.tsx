/* A topic kept alive (design §R8-11): the channels and words it watches for new videos, the new videos offered (要加吗,
   never added by themselves), and its archive once the question has its answer (结论定格, nothing more read or told). */
import { useRef, useState } from "react";
import { Words } from "../../cjk";
import { Sheet, type SheetHandle } from "../Sheet";
import { len, Thumb, when } from "./bits";
import { dayOf } from "./Claims";

export type Channel = { name: string; url: string };
export type Watching = { channels: Channel[]; words: string[]; at: string | null; can: Channel[] };
export type Candidate = { id: string; title: string | null; channel: string | null; duration: number | null; published: string | null; why: string | null; thumb: string };
export type Archived = { at: string; how: How; why: string | null } | null;
export type How = "应验" | "落空" | "部分应验" | "不判";
const HOWS: How[] = ["应验", "落空", "部分应验", "不判"];

/** The header's line for an archived topic: 已归档 · 应验 · 11月9日 — and what was written then */
export const archivedText = (a: NonNullable<Archived>) => ["已归档", a.how, when(a.at)].filter(Boolean).join(" · ");

export function ArchiveSheet({ onClose, onGo }: { onClose: () => void; onGo: (how: How, why: string) => Promise<boolean> }) {
  const [how, setHow] = useState<How | null>(null);
  const [why, setWhy] = useState("");
  const [busy, setBusy] = useState(false);
  const sheet = useRef<SheetHandle>(null);
  const go = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!how || busy) return;
    setBusy(true);
    if (await onGo(how, why.trim())) sheet.current?.close();
    else setBusy(false);
  };
  return (
    <Sheet ref={sheet} title="归档" onClose={onClose} className="tp-sheet">
      <form className="tp-pick tp-arch" onSubmit={go}>
        <div className="tp-arch-how" role="radiogroup" aria-label="问题的结果">
          {HOWS.map((h) => (
            <button key={h} type="button" role="radio" aria-checked={how === h} className={`btn${how === h ? " on" : ""}`} onClick={() => setHow(h)}>{h}</button>
          ))}
        </div>
        <textarea className="tp-notes" value={why} rows={2} maxLength={300} onChange={(e) => setWhy(e.target.value)} placeholder="一句话（可不填）" aria-label="说明" />
        <footer className="tp-pick-foot">
          <p className="tp-sum">结论定格，不再读、不再盯、不再推</p>
          <button type="submit" className="btn solid" disabled={!how || busy}>归档</button>
        </footer>
      </form>
    </Sheet>
  );
}

export function WatchSheet({ w, onClose, onSave }: { w: Watching; onClose: () => void; onSave: (channels: string[], words: string[]) => Promise<boolean> }) {
  const [on, setOn] = useState(() => new Set(w.channels.map((c) => c.url)));
  const [words, setWords] = useState(w.words.join("、"));
  const [busy, setBusy] = useState(false);
  const sheet = useRef<SheetHandle>(null);
  const list = words.split(/[,，、;；\s]+/).map((x) => x.trim()).filter(Boolean);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    if (await onSave([...on], list)) sheet.current?.close();
    else setBusy(false);
  };
  const flip = (url: string) => setOn((s) => { const n = new Set(s); if (n.has(url)) n.delete(url); else n.add(url); return n; });
  return (
    <Sheet ref={sheet} title="盯频道" onClose={onClose} className="tp-sheet">
      <form className="tp-pick tp-watch-f" onSubmit={save}>
        {w.can.length > 0 ? (
          <ul className="tp-watch-c">
            {w.can.map((c) => (
              <li key={c.url}><label><input type="checkbox" checked={on.has(c.url)} onChange={() => flip(c.url)} /><span>{c.name}</span></label></li>
            ))}
          </ul>
        ) : <p className="tp-quiet">这批视频的频道还没读到主页</p>}
        <input className="tp-notes tp-watch-w" value={words} maxLength={200} onChange={(e) => setWords(e.target.value)}
          placeholder={on.size ? "标题里有这些词才问（可不填）" : "关键词：搜 YouTube 的新视频"} aria-label="关键词" />
        <footer className="tp-pick-foot">
          <p className="tp-sum">{on.size || list.length ? "有新视频时问你要不要加" : "不盯"}</p>
          <button type="submit" className="btn solid" disabled={busy}>{on.size || list.length ? "盯着" : "存"}</button>
        </footer>
      </form>
    </Sheet>
  );
}

/** What the topic watches, under its videos: 盯着 利率观察、宏观札记 · 降息 */
export function WatchLine({ w, onEdit }: { w: Watching; onEdit: () => void }) {
  const what = [w.channels.map((c) => c.name).join("、"), w.words.map((x) => `「${x}」`).join("")].filter(Boolean).join(" · ");
  return (
    <p className="tp-watch">
      {what ? <><span>盯着 {what}</span><button type="button" className="link" onClick={onEdit}>改</button></>
        : <button type="button" className="link" onClick={onEdit}>盯频道</button>}
    </p>
  );
}

/** New videos of what the topic watches: 加进来 reads it like any added; 不要 is not asked again */
export function Candidates({ list, onTake }: { list: Candidate[]; onTake: (id: string, take: boolean) => void }) {
  if (!list.length) return null;
  return (
    <section className="tp-cands" aria-labelledby="tp-cands">
      <h3 id="tp-cands">要加吗<span className="num">{list.length}</span></h3>
      <ol>
        {list.map((c) => {
          const why = c.channel && c.why?.startsWith(`${c.channel} · `) ? c.why.slice(c.channel.length + 3) : c.why === c.channel ? "" : c.why;
          return (
          <li key={c.id}>
            <a className="tp-cand-a" href={`https://www.youtube.com/watch?v=${c.id}`} target="_blank" rel="noreferrer">
              <Thumb src={c.thumb} />
              <span className="tp-v-t">
                <b><Words>{c.title || c.id}</Words></b>
                <small>{[c.channel, dayOf(c.published), len(c.duration), why].filter(Boolean).join(" · ")}</small>
              </span>
            </a>
            <span className="tp-cand-b">
              <button type="button" className="btn sm" onClick={() => onTake(c.id, true)}>加进来</button>
              <button type="button" className="btn ghost sm" onClick={() => onTake(c.id, false)}>不要</button>
            </span>
          </li>
          );
        })}
      </ol>
    </section>
  );
}
