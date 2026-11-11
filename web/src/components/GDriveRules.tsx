import { useCallback, useEffect, useRef, useState } from "react";
import { type AuthorRule, type Deletable, gdrive, type GDriveView, HttpError, type Pass, type Rules, type RulesChange, type TopicRule } from "../api";
import { bytes } from "../util";
import { Fold } from "./Fold";
import { Clear } from "./Knobs";
import { Sheet, type SheetHandle } from "./Sheet";

/* 设置 → 网盘备份: which videos go to the drive, and taking away what no rule wants any more. A video goes when any rule
   that covers it says 传 — its author's (members-only and public ones apart) or a topic it is in; one not decided yet
   (待定) does not go, and is not 不传 either. 不传 only stops what is to come: the copies already there are listed under
   可删除, all ticked but the ones that are the last copy left, and go to the drive's trash on a second click. */

/** 传 | 不传, neither lit while it is not decided */
export function PassSeg({ value, onChange, label, disabled }: { value: Pass; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <span className={`seg gd-pass${value == null ? " open" : ""}`} role="radiogroup" aria-label={label}>
      {[true, false].map((v) => (
        <button key={String(v)} type="button" role="radio" aria-checked={value === v} className={value === v ? "on" : ""} disabled={disabled}
          onClick={() => { if (value !== v) onChange(v); }}>{v ? "传" : "不传"}</button>
      ))}
    </span>
  );
}

type Field = "member" | "public";
const FIELDS: [Field, string][] = [["member", "会员"], ["public", "公开"]];
type Changes = { authors: Map<string, { member?: boolean; public?: boolean }>; topics: Map<number, boolean> };
const blank = (): Changes => ({ authors: new Map(), topics: new Map() });
const any = (c: Changes) => c.authors.size > 0 || c.topics.size > 0;
const bodyOf = (c: Changes): RulesChange => ({
  ...(c.authors.size ? { authors: [...c.authors].map(([id, x]) => ({ id, ...x })) } : {}),
  ...(c.topics.size ? { topics: [...c.topics].map(([id, backup]) => ({ id, backup })) } : {}),
});

/** The rules, read when a sheet opens. A click shows at once and is saved at once, one request at a time: what is clicked
 *  while one is out goes with the next, so the box takes them in the order they were made. One that fails is undone by
 *  reading the rules again — unless a save goes out or comes back meanwhile, whose answer is newer than that read.
 *  Closing the sheet does not drop what is still to be saved; `saved` follows the last one, `lost` says one that failed
 *  once the sheet is gone. */
function useRules(saved: () => void, lost: (text: string) => void) {
  const [data, setData] = useState<Rules | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const flying = useRef<Changes | null>(null);
  const next = useRef<Changes>(blank());
  const alive = useRef(true);
  const moved = useRef(0);  // saves sent and answered: a read asked before the last of them is out of date
  const [, bump] = useState(0);
  const load = useCallback(() => {
    const at = moved.current;
    const fresh = () => alive.current && at === moved.current;
    gdrive.rules().then((r) => { if (fresh()) setData(r); }, (e) => { if (fresh()) setErr((e as Error).message); });
  }, []);
  useEffect(() => {
    alive.current = true;
    load();
    return () => { alive.current = false; };
  }, [load]);
  const flush = () => {
    if (flying.current || !any(next.current)) return;
    const c = next.current;
    next.current = blank();
    flying.current = c;
    moved.current++;
    gdrive.setRules(bodyOf(c)).then((r) => {
      moved.current++;
      flying.current = null;
      if (alive.current) { setData(r); setErr(null); }
      if (any(next.current)) flush(); else saved();
    }, (e) => {
      moved.current++;
      flying.current = null;
      next.current = blank();
      saved();
      if (alive.current) { setErr((e as Error).message); load(); } else lost((e as Error).message);
    }).finally(() => { if (alive.current) bump((n) => n + 1); });
  };
  const setAuthors = (ids: string[], f: Field, v: boolean) => {
    for (const id of ids) next.current.authors.set(id, { ...next.current.authors.get(id), [f]: v });
    bump((n) => n + 1);
    flush();
  };
  const setTopics = (ids: number[], v: boolean) => {
    for (const id of ids) next.current.topics.set(id, v);
    bump((n) => n + 1);
    flush();
  };
  // what is shown: what the box said, with what is on its way to it
  const author = (a: AuthorRule): AuthorRule => ({ ...a, ...flying.current?.authors.get(a.id), ...next.current.authors.get(a.id) });
  const topic = (t: TopicRule): TopicRule => {
    const v = next.current.topics.get(t.id) ?? flying.current?.topics.get(t.id);
    return v === undefined ? t : { ...t, backup: v };
  };
  return { data, err, setAuthors, setTopics, author, topic };
}

/** how many videos it has here; how much of it is on the drive */
const sizeOf = (videos: number, drive: { videos: number; bytes: number }) => `${videos} 期${drive.videos ? ` · 网盘 ${bytes(drive.bytes)}` : ""}`;
/** a column all 传, all 不传, or neither */
const allOf = (vs: Pass[]): Pass => (vs.length && vs.every((v) => v === true) ? true : vs.length && vs.every((v) => v === false) ? false : null);

/** 全部传 · 全部不传 over a column: lit when the whole column is so */
function All({ value, onChange, label }: { value: Pass; onChange: (v: boolean) => void; label: string }) {
  return (
    <span className="gd-all" role="group" aria-label={label}>
      <button type="button" aria-pressed={value === true} className={value === true ? "on" : ""} onClick={() => onChange(true)}>全部传</button>
      <button type="button" aria-pressed={value === false} className={value === false ? "on" : ""} onClick={() => onChange(false)}>全部不传</button>
    </span>
  );
}

function Waiting({ err }: { err: string | null }) {
  return err ? <p className="gd-err" role="alert">{err}</p> : <p className="gd-wait"><i className="spin" aria-hidden />正在读取…</p>;
}

/** The authors that go in a group: their columns' 全部, then one line each (two on a phone: the name, then the switches) */
function AuthorList({ rows, set }: { rows: AuthorRule[]; set: (ids: string[], f: Field, v: boolean) => void }) {
  return (
    <>
      <div className="gd-ah">
        <span />
        {FIELDS.map(([f, name]) => (
          <span key={f} className="gd-c">
            <span className="gd-cn">{name}</span>
            <All value={allOf(rows.map((a) => a[f]))} label={`${name}：全部`} onChange={(v) => set(rows.map((a) => a.id), f, v)} />
          </span>
        ))}
      </div>
      <ul className="gd-list">
        {rows.map((a) => (
          <li key={a.id} className={`gd-ar${a.member == null || a.public == null ? " open" : ""}`}>
            <span className="gd-n">
              <b title={a.name}>{a.name}</b>
              <small>{sizeOf(a.videos, a.drive)}</small>
            </span>
            {FIELDS.map(([f, name]) => (
              <span key={f} className="gd-c">
                <span className="gd-cl" aria-hidden>{name}</span>
                <PassSeg value={a[f]} label={`${a.name}：${name}`} onChange={(v) => set([a.id], f, v)} />
              </span>
            ))}
          </li>
        ))}
      </ul>
    </>
  );
}

/** 作者: the 频道's first; those only in topics folded under 其他作者, by how many videos they have */
export function AuthorsSheet({ onClose, saved, lost }: { onClose: () => void; saved: () => void; lost: (text: string) => void }) {
  const r = useRules(saved, lost);
  const [others, setOthers] = useState(false);
  const all = (r.data?.authors || []).map(r.author);
  const mine = all.filter((a) => a.channel), rest = all.filter((a) => !a.channel);
  return (
    <Sheet title="作者" tall onClose={onClose} className="gd-sheet">
      <div className="gd-rules">
        {!r.data ? <Waiting err={r.err} /> : (
          <>
            {mine.length > 0 && <AuthorList rows={mine} set={r.setAuthors} />}
            {rest.length > 0 && (
              <section className="gd-others">
                <button type="button" className="gd-more" aria-expanded={others} onClick={() => setOthers(!others)}>
                  其他作者 {rest.length} 位<i className={`chev${others ? " up" : ""}`} aria-hidden />
                </button>
                <Fold open={others}><AuthorList rows={rest} set={r.setAuthors} /></Fold>
              </section>
            )}
            {r.err && <p className="gd-err" role="alert">{r.err}</p>}
          </>
        )}
      </div>
    </Sheet>
  );
}

export function TopicsSheet({ onClose, saved, lost }: { onClose: () => void; saved: () => void; lost: (text: string) => void }) {
  const r = useRules(saved, lost);
  const rows = (r.data?.topics || []).map(r.topic);
  return (
    <Sheet title="专题" tall onClose={onClose} className="gd-sheet">
      <div className="gd-rules">
        {!r.data ? <Waiting err={r.err} /> : (
          <>
            {rows.length > 0 && (
              <>
                <div className="gd-ah gd-one">
                  <span />
                  <span className="gd-c"><All value={allOf(rows.map((t) => t.backup))} label="全部专题" onChange={(v) => r.setTopics(rows.map((t) => t.id), v)} /></span>
                </div>
                <ul className="gd-list">
                  {rows.map((t) => (
                    <li key={t.id} className={`gd-ar gd-one${t.backup == null ? " open" : ""}`}>
                      <span className="gd-n">
                        <b className="q" title={t.question}>{t.question}</b>
                        <small>{sizeOf(t.videos, t.drive)}</small>
                      </span>
                      <span className="gd-c"><PassSeg value={t.backup} label={t.question} onChange={(v) => r.setTopics([t.id], v)} /></span>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {r.err && <p className="gd-err" role="alert">{r.err}</p>}
          </>
        )}
      </div>
    </Sheet>
  );
}

/** `by`: whose it is, where the group is not an author */
type Copy = { id: string; title: string | null; bytes: number; by?: string };

/** A group of copies, ticked or not as a whole from its head */
function Group({ name, copies, picked, toggle, setMany, quiet }: {
  name: React.ReactNode; copies: Copy[]; picked: Set<string>; toggle: (id: string) => void; setMany: (ids: string[], on: boolean) => void; quiet?: boolean;
}) {
  const box = useRef<HTMLInputElement>(null);
  const n = copies.filter((c) => picked.has(c.id)).length;
  useEffect(() => { if (box.current) box.current.indeterminate = n > 0 && n < copies.length; }, [n, copies.length]);
  const size = copies.reduce((s, c) => s + c.bytes, 0);
  return (
    <section className={`gd-grp${quiet ? " last" : ""}`}>
      <label className="gd-gh">
        <input ref={box} type="checkbox" checked={n === copies.length} onChange={() => setMany(copies.map((c) => c.id), n < copies.length)} />
        <b>{name}</b>
        <span className="gd-s">{copies.length} 期 · {bytes(size)}</span>
      </label>
      <ul>
        {copies.map((c) => (
          <li key={c.id}>
            <label>
              <input type="checkbox" checked={picked.has(c.id)} onChange={() => toggle(c.id)} />
              <span className="gd-t" title={c.title || c.id}>{c.title || <span className="mono">{c.id}</span>}{c.by && <small>{c.by}</small>}</span>
              <span className="gd-s">{bytes(c.bytes)}</span>
            </label>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** 可删除: the copies no rule wants any more, by author, all ticked; the last copy of a video gone from YouTube and from
 *  the box apart and not ticked. They go to the drive's trash in the background, on a second click; `onSent` has the
 *  page show how far it has got as soon as the box has taken them, the sheet still open or closed meanwhile. */
export function TrashSheet({ onClose, onSent, reload }: { onClose: () => void; onSent: (g: GDriveView) => void; reload: () => void }) {
  const [d, setD] = useState<Deletable | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const sheet = useRef<SheetHandle>(null);
  useEffect(() => {
    let alive = true;
    gdrive.deletable().then((x) => {
      if (!alive) return;
      setD(x);
      setPicked(new Set((x.authors || []).flatMap((a) => a.videos.map((v) => v.id))));
    }, (e) => { if (alive) setErr((e as Error).message); });
    return () => { alive = false; };
  }, []);
  const authors = d?.authors || [], kept = d?.kept || [];
  const last: Copy[] = (d?.last_copy || []).map((c) => ({ id: c.id, title: c.title, bytes: c.bytes, by: c.author?.name }));
  const every: Copy[] = [...authors.flatMap((a) => a.videos), ...last];
  const chosen = every.filter((c) => picked.has(c.id));
  const size = chosen.reduce((s, c) => s + c.bytes, 0);
  const toggle = (id: string) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const setMany = (ids: string[], on: boolean) => setPicked((p) => { const n = new Set(p); ids.forEach((id) => (on ? n.add(id) : n.delete(id))); return n; });
  const go = async () => {
    setBusy(true);
    setErr(null);
    try {
      const g = await gdrive.trash(chosen.map((c) => c.id));
      onSent(g);
      sheet.current?.close();
    } catch (e) {
      // 409: a batch is already being moved (from another device, say): the page shows how far it has got
      setErr(e instanceof HttpError && e.status === 409 ? e.message || "上一批还在移，移完再来" : (e as Error).message);
      setBusy(false);
      reload();
    }
  };
  const what = `${chosen.length} 期 · ${bytes(size)}`;
  return (
    <Sheet ref={sheet} title="可删除" tall onClose={onClose} className="gd-sheet">
      <div className="gd-trash">
        {!d ? <Waiting err={err} /> : (
          <div className="gd-groups">
            {authors.map((a) => <Group key={a.id} name={a.name} copies={a.videos} picked={picked} toggle={toggle} setMany={setMany} />)}
            {last.length > 0 && <Group name="已下架 · 只剩这一份" copies={last} picked={picked} toggle={toggle} setMany={setMany} quiet />}
            {kept.length > 0 && (
              <ul className="gd-kept">
                {kept.map((k) => (
                  <li key={"topic" in k ? `t${k.topic.id}` : `a${k.author.id}`}>
                    {"topic" in k ? `另 ${k.videos} 期还在专题《${k.topic.question}》里，留着` : `另 ${k.videos} 期是「${k.author.name}」的，留着`}
                  </li>
                ))}
              </ul>
            )}
            {!every.length && <p className="gd-wait">没有可删除的副本</p>}
          </div>
        )}
        {d && err && <p className="gd-err" role="alert">{err}</p>}
        <footer className="gd-foot">
          <p>进 Google 回收站，30 天内可在网盘里恢复；清空回收站才腾出空间</p>
          <Clear solid disabled={!chosen.length || busy} onClear={go} label={`移到回收站${chosen.length ? ` ${what}` : ""}`} sure={`确认移到回收站 ${what}`} />
        </footer>
      </div>
    </Sheet>
  );
}
