/* The answer as claims (design §R8-10): each claim of one of four kinds, citing its videos, with how sure it is; the
   disputes between them, right under the conclusion; the predictions with the day they fall due; the claims settled or
   replaced; the sources with what each stands to gain, what it relays and how its channel's predictions went; and the
   versions the answer has been through. A claim can be marked by the user (a prediction 应验 or 落空, a claim 被取代 or
   不算): the page shows the mark at once, every answer after keeps it. */
import { useEffect, useRef, useState } from "react";
import { Words } from "../../cjk";
import { linkProps } from "../../router";
import { type Item, when } from "./bits";
import { type ChipWays, renderCited, uncited } from "./cite";

export type Level = "高" | "中" | "低" | "很低" | "";
export type Kind = "事实" | "解读" | "预测" | "建议";
export type Status = "现行" | "被取代" | "落空" | "应验" | "不算";
export type Claim = {
  id: string; type: Kind; text: string; v: number[]; independent: number; certainty: Level; why?: string; status: Status;
  by?: string; shift?: boolean; due?: string; condition?: string; vague?: boolean; premise?: string; marked?: { why?: string | null; at?: string };
};
export type Dispute = { id: string; about: string; claims: string[]; decide?: string; lean?: string };
export type Source = { v: number; stance?: string; relays?: string; interests?: string };
export type Section = { title: string; body?: string; claims?: string[]; note?: string };
export type Synth = {
  answer: string; certainty?: { level: Level; why?: string }; claims?: Claim[]; disputes?: Dispute[]; sources?: Source[]; balance?: string;
  changes?: string[]; answer_changed?: boolean; first: { v: number; why: string }[]; sections: Section[]; videos: { v: number; adds: string }[];
};
export type Version = { n: number; at: string; read: number; total: number; changes: string[]; certainty: Level | null; answer_changed: boolean };
/** A channel's predictions over every topic: how many came true, how many did not, how many are still open */
export type Record_ = { hit: number; miss: number; open: number };
export type Mark = (claim: string, status: Status, why?: string) => void;

const LV: Record<string, string> = { 高: "hi", 中: "mid", 低: "lo", 很低: "vlo" };
const today = () => new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10);  // the box keeps Shanghai's days

export const dayOf = (p?: string | null) => {
  const m = /^(\d{4})-?(\d{2})-?(\d{2})/.exec(p || "");
  return m ? `${m[1]}-${m[2]}-${m[3]}` : "";
};

/** To a claim or a dispute of the answer, without a step in the history */
function jump(e: React.MouseEvent, id: string) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  e.preventDefault();
  const el = document.getElementById(id);
  if (!el) return;
  el.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
  el.classList.remove("tp-hit");
  void (el as HTMLElement).offsetWidth;
  el.classList.add("tp-hit");
}

export function Certainty({ c }: { c?: { level: Level; why?: string } }) {
  if (!c?.level) return null;
  return (
    <p className="tp-cert">
      <span className={`tp-lv ${LV[c.level]}`}>确定性 {c.level}</span>
      {c.why && <span>{c.why}</span>}
    </p>
  );
}

/** Who said it: V2 · 频道 · 2026-09-12 (the first video the claim cites) */
function Who({ c, byNo }: { c: Claim; byNo: Map<number, Item> }) {
  const it = byNo.get(c.v[0]);
  return <span className="tp-who">{[`V${c.v[0]}${c.v.length > 1 ? ` 等 ${c.v.length} 期` : ""}`, it?.channel, dayOf(it?.published)].filter(Boolean).join(" · ")}</span>;
}

/** The user's say on a claim: a prediction came true or did not; a claim is replaced or does not count; or the mark undone */
function Marker({ c, onMark }: { c: Claim; onMark: Mark }) {
  const [open, setOpen] = useState(false);
  const [why, setWhy] = useState("");
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const off = (e: PointerEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", off);
    return () => document.removeEventListener("pointerdown", off);
  }, [open]);
  if (c.marked) return <button type="button" className="tp-mk-x" onClick={() => onMark(c.id, "现行")}>撤销</button>;
  const ways: Status[] = c.type === "预测" ? ["应验", "落空", "不算"] : ["被取代", "不算"];
  const go = (s: Status) => { setOpen(false); onMark(c.id, s, why.trim() || undefined); setWhy(""); };
  return (
    <span className="tp-mk" ref={box}>
      <button type="button" className="tp-mk-b" aria-expanded={open} aria-label={`标记 ${c.id}`} title="标记" onClick={() => setOpen(!open)}>
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden><circle cx="3" cy="7" r="1.2" /><circle cx="7" cy="7" r="1.2" /><circle cx="11" cy="7" r="1.2" /></svg>
      </button>
      {open && (
        <span className="tp-mk-p" role="group" aria-label={`标记 ${c.id}`}>
          <input value={why} onChange={(e) => setWhy(e.target.value)} placeholder="理由（可不填）" maxLength={300} aria-label="理由" autoFocus
            onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }} />
          <span className="tp-mk-w">{ways.map((s) => <button key={s} type="button" className="btn sm" onClick={() => go(s)}>{s}</button>)}</span>
        </span>
      )}
    </span>
  );
}

function ClaimLine({ c, html, dispute, onMark }: { c: Claim; html: string; dispute?: string; onMark: Mark }) {
  const past = c.type === "预测" && !!c.due && c.due < today();
  return (
    <li id={c.id} className="tp-cl">
      <span className="tp-ty">{c.type}</span>
      <div className="tp-cl-b">
        <div className="tp-cl-t" dangerouslySetInnerHTML={{ __html: html }} />
        <div className="tp-cl-m">
          {c.certainty && <span className={`tp-lv ${LV[c.certainty]}`} title={c.why || undefined}>{c.certainty}</span>}
          {c.certainty && c.certainty !== "高" && c.why && <span>{c.why}</span>}
          {c.type === "预测" && c.due && <span className={past ? "tp-past" : ""}>{past ? "已到期" : "到期"} {c.due}</span>}
          {c.vague && !(c.why || "").includes("时间含糊") && <span>时间含糊</span>}
          {c.type === "预测" && c.condition && <span>怎么算应验：{c.condition}</span>}
          {c.type === "建议" && c.premise && <span>前提：{c.premise}</span>}
          {dispute && <a className="tp-see" href={`#${dispute}`} onClick={(e) => jump(e, dispute)}>见分歧</a>}
        </div>
      </div>
      <Marker c={c} onMark={onMark} />
    </li>
  );
}

/** The answer below its conclusion, when it is made of claims: the disputes, then `first` (先看), the sub-questions,
 *  the predictions still open, the claims settled or replaced, the sources, the versions. */
export function ClaimAnswer({ s, tid, byNo, ways, versions, record, first, onMark }: {
  s: Synth; tid: number; byNo: Map<number, Item>; ways: ChipWays; versions: Version[]; record: Record<string, Record_>;
  first: React.ReactNode; onMark: Mark;
}) {
  const claims = s.claims || [];
  const byId = new Map(claims.map((c) => [c.id, c]));
  const html = new Map(claims.map((c) => [c.id, renderCited(c.text, ways, true)]));
  const disputeOf = new Map<string, string>();
  for (const d of s.disputes || []) for (const id of d.claims) if (!disputeOf.has(id)) disputeOf.set(id, d.id);
  const live = (c: Claim) => c.status === "现行";
  const open = claims.filter((c) => c.type === "预测" && live(c)).sort((a, b) => (a.due || "9").localeCompare(b.due || "9"));
  const done = claims.filter((c) => !live(c));
  const settled = done.filter((c) => c.status === "应验" || c.status === "落空");
  const used = new Map((s.sources || []).map((x) => [x.v, x]));
  const adds = new Map((s.videos || []).map((x) => [x.v, x.adds]));
  const nos = [...new Set([...(s.videos || []).map((x) => x.v), ...used.keys()])].filter((no) => byNo.has(no));
  const reason = (c: Claim) => {
    if (c.marked) return c.marked.why ? `你标的：${c.marked.why}` : "你标的";
    const by = c.by ? byId.get(c.by) : undefined;
    return by ? `${c.shift ? "后来改说" : c.status === "被取代" ? "新的说法" : "依据"}：${uncited(by.text)}` : "";
  };
  return (
    <>
      {(s.disputes || []).length > 0 && (
        <section className="tp-disp" aria-labelledby="tp-disp">
          <h2 className="tp-h" id="tp-disp">分歧</h2>
          {s.disputes!.map((d) => (
            <div key={d.id} id={d.id} className="tp-d">
              <h3><Words>{d.about}</Words></h3>
              <ul>
                {d.claims.map((id) => byId.get(id)).filter((c): c is Claim => !!c).map((c) => (
                  <li key={c.id}>
                    <Who c={c} byNo={byNo} />
                    <div className="tp-cl-t" dangerouslySetInnerHTML={{ __html: html.get(c.id) || "" }} />
                  </li>
                ))}
              </ul>
              {d.decide && <p><b>能判定</b>{d.decide}</p>}
              {d.lean && <p><b>倾向</b>{d.lean}</p>}
            </div>
          ))}
        </section>
      )}
      {first}
      {s.sections.map((x, i) => {
        const cs = (x.claims || []).map((id) => byId.get(id)).filter((c): c is Claim => !!c && live(c));
        return (cs.length > 0 || x.note) && (
          <section key={i} className="tp-sec">
            <h2><Words>{x.title}</Words></h2>
            {x.note && <p className="tp-quiet">{x.note}</p>}
            {cs.length > 0 && (
              <ul className="tp-cls">
                {cs.map((c) => <ClaimLine key={c.id} c={c} html={html.get(c.id) || ""} dispute={disputeOf.get(c.id)} onMark={onMark} />)}
              </ul>
            )}
          </section>
        );
      })}
      {open.length > 0 && (
        <section className="tp-pred" aria-labelledby="tp-pred">
          <h2 className="tp-h" id="tp-pred">待验证的预测</h2>
          <ol>
            {open.map((c) => {
              const past = !!c.due && c.due < today();
              return (
                <li key={c.id} className={past ? "past" : ""}>
                  <span className="tp-due num">{c.due || "时间含糊"}</span>
                  <span className="tp-pred-t">
                    <a href={`#${c.id}`} onClick={(e) => jump(e, c.id)}>{uncited(c.text)}</a>
                    <Who c={c} byNo={byNo} />
                  </span>
                  <Marker c={c} onMark={onMark} />
                </li>
              );
            })}
          </ol>
        </section>
      )}
      {done.length > 0 && (
        <section className="tp-old" aria-labelledby="tp-old">
          <h2 className="tp-h" id="tp-old">{settled.length === done.length ? "已结算" : settled.length ? "已结算 · 已被取代" : "已被取代"}</h2>
          <ul>
            {[...settled, ...done.filter((c) => !settled.includes(c))].map((c) => (
              <li key={c.id} id={c.id}>
                <span className="tp-st">{c.shift ? "改口" : c.status}</span>
                <div className="tp-cl-b">
                  <div className="tp-cl-t" dangerouslySetInnerHTML={{ __html: html.get(c.id) || "" }} />
                  <div className="tp-cl-m">
                    <Who c={c} byNo={byNo} />
                    {reason(c) && <span>{reason(c)}</span>}
                  </div>
                </div>
                {c.marked && <Marker c={c} onMark={onMark} />}
              </li>
            ))}
          </ul>
        </section>
      )}
      {nos.length > 0 && (
        <section className="tp-adds tp-srcs" aria-labelledby="tp-srcs">
          <h2 className="tp-h" id="tp-srcs">来源</h2>
          {s.balance && <p className="tp-bal">{s.balance}</p>}
          <ol>
            {nos.map((no, i) => {
              const it = byNo.get(no)!, x = used.get(no);
              const r = it.channel && nos.findIndex((o) => byNo.get(o)!.channel === it.channel) === i ? record[it.channel] : undefined;
              const more = [x?.interests && `利益相关：${x.interests}`, x?.relays && `转述：${x.relays}`,
                r && r.hit + r.miss > 0 && `这个频道的预测 ${r.hit} 应验 · ${r.miss} 落空`].filter(Boolean);
              return (
                <li key={no}>
                  <a {...linkProps(`/topics/${tid}/${it.id}`)}>
                    <span className="no num">V{no}</span>
                    <span className="t">
                      <span>{uncited(adds.get(no) || it.gist || "")}</span>
                      <small>{[it.title || it.id, it.channel, dayOf(it.published), x?.stance].filter(Boolean).join(" · ")}</small>
                      {more.length > 0 && <small className="tp-src-m">{more.join("　")}</small>}
                    </span>
                  </a>
                </li>
              );
            })}
          </ol>
        </section>
      )}
      {versions.length > 1 && (
        <details className="tp-vers">
          <summary>{versions.length} 个版本</summary>
          <ol>
            {versions.map((x) => (
              <li key={x.n}>
                <span className="num">第 {x.n} 版</span>
                <span>{[when(x.at), `${x.read}/${x.total} 期`, x.certainty && `确定性 ${x.certainty}`].filter(Boolean).join(" · ")}</span>
                {x.changes.length > 0 && <span className="tp-vers-c">{x.changes.join("；")}</span>}
              </li>
            ))}
          </ol>
        </details>
      )}
    </>
  );
}
