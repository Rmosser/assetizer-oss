import { Fragment, useState } from "react";
import { media } from "../../api";
import { linkProps } from "../../router";
import { Adopt, fmt, Icon, solo, TYPE_ICON, ytEmbed, ytId } from "./bits";
import { keys } from "./lit";
import type { Block, Compare, Part, Preview, PropRow, Seg } from "./types";

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
export function notionDate(v: string) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) { const [y, m, d] = v.split("-").map(Number); return `${MONTHS[m - 1]} ${d}, ${y}`; }
  const t = new Date(v);
  if (Number.isNaN(+t)) return v;
  const h = t.getHours();
  return `${MONTHS[t.getMonth()]} ${t.getDate()}, ${t.getFullYear()} ${h % 12 || 12}:${String(t.getMinutes()).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}
const TAG: Record<string, string> = { split: "拆出", merge: "合并", new: "新" };

export function Rich({ segs }: { segs?: Seg[] }) {
  return <>{(segs || []).map((s, i) => {
    let n: React.ReactNode = s.t;
    if (s.c) n = <code className="code">{n}</code>;
    if (s.b) n = <b>{n}</b>;
    if (s.i) n = <i>{n}</i>;
    if (s.href) n = <a href={s.href} target="_blank" rel="noreferrer" className="pl">{n}</a>;
    return <Fragment key={i}>{n}</Fragment>;
  })}</>;
}

export function Opt({ name, color, bad }: { name: string; color?: string; bad?: boolean }) {
  return <span className={`nt-opt ${color || "default"}${bad ? " bad" : ""}`}>{name}</span>;
}

/** Parts of a filled-in template, each variable keyed to the title token (and the source field) it comes from. */
export function Parts({ parts, fieldOf }: { parts: Part[]; fieldOf: (v: string) => string | null }) {
  return <>{parts.map((p, i) => (p.var
    ? <span key={i} className="tp lt-bg" data-k={keys(`tok:${p.var}`, fieldOf(p.var))} data-side="dst">{p.text}</span>
    : <Fragment key={i}>{p.text}</Fragment>))}</>;
}

function Value({ r }: { r: PropRow }) {
  if (!r.writable) return <span className="empty">{["created_time", "last_edited_time"].includes(r.type) ? "写入后由 Notion 填写" : "Notion 自动计算"}</span>;
  const v = r.value;
  if (!r.mapping || v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return <span className="empty">Empty</span>;
  if (r.type === "select" || r.type === "status") return <Opt name={String(v)} color={r.colors[String(v)]} bad={!!r.problem} />;
  if (r.type === "multi_select") return <>{(v as string[]).map((x) => <Opt key={x} name={x} color={r.colors[x]} bad={!!r.problem} />)}</>;
  if (r.type === "relation") return <>{(v as { id: string; title: string }[]).map((p) => <span key={p.id} className="pl pg">{p.title || "Untitled"}</span>)}</>;
  if (r.type === "date") return <span>{notionDate(String(v))}</span>;
  if (r.type === "url") return <a className="pl" href={String(v)} target="_blank" rel="noreferrer">{String(v)}</a>;
  if (r.type === "checkbox") return <span>{v ? "☑" : "☐"}</span>;
  return <span className="txt">{String(v)}</span>;
}

/** A clip on the page: the file delivery uploads, loaded only when played. */
function Clip({ b }: { b: Block }) {
  const [on, setOn] = useState(false);
  const tc = b.caption?.split(" · ")[0];
  return (
    <figure className="b-vid-f">
      {on && b.asset_id ? <div className="b-vid on"><video src={media(b.asset_id)} controls autoPlay playsInline onPlay={solo} /></div> : (
        <button className="b-vid dark" disabled={!b.asset_id} onClick={() => setOn(true)} aria-label={tc ? `播放 ${tc}` : "播放"}>
          <span className="pl0"><span className="pb"><Icon name="play" size={18} fill /></span>{tc && <b className="tc">{tc}</b>}</span>
        </button>
      )}
      {b.caption && <figcaption>{b.caption}</figcaption>}
    </figure>
  );
}

/** An embedded YouTube video: the player loads only when played. */
function Embed({ url }: { url?: string }) {
  const [on, setOn] = useState(false);
  const id = ytId(url);
  if (on && id) {
    return <div className="b-vid on"><iframe src={ytEmbed(id)} title="YouTube" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowFullScreen referrerPolicy="strict-origin-when-cross-origin" /></div>;
  }
  return (
    <button className="b-vid dark" disabled={!id} onClick={() => setOn(true)} aria-label="播放 YouTube">
      <span className="pl0"><span className="pb"><Icon name="play" size={18} fill /></span><b className="tc">YouTube</b></span>
    </button>
  );
}

function BlockView({ b, k, tag, heading, fieldOf }: { b: Block; k?: string; tag?: string; heading?: Part[]; fieldOf: (v: string) => string | null }) {
  const t = b.type;
  const body = (() => {
    if (t === "heading_1") return <div className="b-h1">{heading?.length ? <Parts parts={heading} fieldOf={fieldOf} /> : <Rich segs={b.text} />}</div>;
    if (t === "heading_2" || t === "heading_3") {
      const C = t === "heading_2" ? "b-h2" : "b-h3";
      return <div className={C}>{tag && <span className="mk">{tag}</span>}<span><Rich segs={b.text} /></span></div>;
    }
    if (t === "paragraph") return <div className="b-p"><Rich segs={b.text} /></div>;
    if (t === "bulleted_list_item") return <div className="b-li"><Rich segs={b.text} /></div>;
    if (t === "to_do") return <div className="b-todo"><span>{b.checked ? "☑" : "☐"}</span><Rich segs={b.text} /></div>;
    if (t === "quote") return <div className="b-q"><Rich segs={b.text} /></div>;
    if (t === "divider") return <div className="b-div" />;
    if (t === "callout") return <div className="b-call"><span>{b.emoji}</span><div><Rich segs={b.text} /></div></div>;
    if (t === "code") return <pre className="b-code"><Rich segs={b.text} /></pre>;
    if (t === "toggle") return (
      <details className="b-tg"><summary><Rich segs={b.text} /><span className="cnt">{b.children?.length ? `${b.children.length} 条` : ""}</span></summary>
        <div className="kids">{(b.children || []).map((c, i) => <BlockView key={i} b={c} fieldOf={fieldOf} />)}</div></details>
    );
    if (t === "table") return (
      <table className="b-tab"><tbody>{(b.rows || []).map((row, ri) => (
        <tr key={ri}>{row.map((cell, ci) => (b.header && ri === 0 ? <th key={ci}><Rich segs={cell} /></th> : <td key={ci}><Rich segs={cell} /></td>))}</tr>
      ))}</tbody></table>
    );
    if (t === "embed") return <Embed key={b.url} url={b.url} />;  // another video: back to the poster, not autoplaying it
    if (t === "image") return (
      <figure className="b-img">{b.asset_id ? <img src={media(b.asset_id, 640)} alt={b.caption || ""} loading="lazy" /> : <span className="ph">截图</span>}
        {b.caption && <figcaption>{b.caption}</figcaption>}</figure>
    );
    if (t === "video") return <Clip key={b.asset_id ?? ""} b={b} />;
    return <div className="b-p"><Rich segs={b.text} /></div>;
  })();
  return <div className={`blk lt-bg${b.tpl ? " tpl" : ""}`} data-k={k} data-side={k ? "dst" : undefined}>{body}</div>;
}

export type PaneHead = {
  version: string; adopted: { pinned: boolean } | null; canAdopt: boolean; canPin: boolean; canUnpin: boolean;
  onAdopt: () => void; onPin: () => void; onUnpin: () => void;
  cmpVersion: number | null; cmp: boolean; onCmp: () => void; link: { href: string; text: string; external: boolean } | null;
};

/** Notion 页面: what this run becomes in the library under the settings on the left, before anything is written. */
export function NotionPane({ preview, error, loading, head, cmp, secOf, propKey, titleKey, fieldOf, paneRef, empty, switcher }: {
  preview: Preview | null; error: string | null; loading: boolean; head: PaneHead | null; cmp: Compare | null;
  secOf: Map<string, number>; propKey: (r: PropRow) => string; titleKey: string; fieldOf: (v: string) => string | null;
  paneRef: React.RefObject<HTMLDivElement | null>; empty?: React.ReactNode; switcher?: React.ReactNode;
}) {
  const changes = new Map((cmp?.b_units || []).map((u) => [u.id, u.change]));
  let n = 0;
  const p = preview;
  const titleText = p?.title.map((x) => x.text).join("") || "";
  return (
    <div className="ch-pane ch-nt">
      <div className="ch-ph">
        {switcher || <span className="pt">Notion 页面</span>}
        {head && <span className="ch-chip sm">{head.version}</span>}
        {head?.adopted && <span className="ch-adopted"><Adopt pinned={head.adopted.pinned} />{head.adopted.pinned ? "采用" : "采用 · 最新完成"}</span>}
        {head && (
          <div className="ch-pha">
            {head.canPin && <button className="ch-phb" onClick={head.onPin}>钉住这一版</button>}
            {head.canUnpin && <button className="ch-phb ghost" onClick={head.onUnpin}>跟随最新完成</button>}
            {head.canAdopt && <button className="ch-phb" onClick={head.onAdopt}>采用这一版</button>}
            {head.cmpVersion != null && (
              <button className={`ch-phb${head.cmp ? " on" : ""}`} aria-pressed={head.cmp} onClick={head.onCmp}><Icon name="swap" size={12} /><span>对比 v{head.cmpVersion}</span></button>
            )}
            {head.link && (head.link.external
              ? <a className="ch-phl" href={head.link.href} target="_blank" rel="noreferrer">{head.link.text} ↗</a>
              : <a className="ch-phl" {...linkProps(head.link.href)}>{head.link.text} ↗</a>)}
          </div>
        )}
      </div>
      <div className={`ch-pw nt${loading ? " loading" : ""}`} ref={paneRef}>
        {error && <p className="ch-err pad">{error}</p>}
        {!p && !error && (empty || <div className="nt-skel" aria-busy="true"><i /><i /><i /></div>)}
        {p && (
          <>
            <div className="ntop">
              <span className="ncrumb"><Icon name="db" size={14} /><span>{p.database.title}</span><span className="sl">/</span><span>{titleText || "Untitled"}</span></span>
              <span className="nst"><span>Share</span><Icon name="ext" size={14} /></span>
            </div>
            <div className="nb">
              {cmp && (
                <div className="cmpbar">
                  <b>v{head?.cmpVersion} → {head?.version}</b>
                  <span className="k"><i className="nw" />新 {cmp.counts.new}</span>
                  <span className="k"><i className="sp" />拆出 {cmp.counts.split}</span>
                  <span className="k"><i className="mg" />合并 {cmp.counts.merge}</span>
                  <span className="k"><i className="gn" />不再单列 {cmp.counts.gone}</span>
                  {cmp.a_units.filter((u) => u.change === "gone").map((u) => (
                    <span key={u.id} className="cg lt-bg" data-k={`a:${u.id}`} data-side="dst"><em>{fmt(u.start)}</em><span>{u.title}</span></span>
                  ))}
                </div>
              )}
              {p.icon && <div className="nic">{p.icon.emoji || <img src={p.icon.url} alt="" />}</div>}
              <h1 className="nti lt-bg" data-k={titleKey} data-side="dst">{p.title.length ? <Parts parts={p.title} fieldOf={fieldOf} /> : "Untitled"}</h1>
              <div className="props">
                {p.properties.filter((r) => r.type !== "title").map((r) => (
                  <div key={r.id} className={`prop lt-bg${r.problem ? " bad" : ""}`} data-k={propKey(r)} data-side="dst">
                    <span className="pn"><Icon name={TYPE_ICON[r.type] || "text"} size={15} /><span>{r.name}</span></span>
                    <span className="pv"><Value r={r} /></span>
                  </div>
                ))}
              </div>
              <div className="cmt"><i>R</i>Add a comment…</div>
              {p.body_error && <p className="b-p empty">{p.body_error}</p>}
              {p.blocks.map((b) => {
                const sec = b.unit_ids.length ? secOf.get(b.unit_ids[0]) : undefined;
                const k = b.origin === "unit" && sec !== undefined
                  ? keys(`c:${sec}`, ...b.unit_ids.map((u) => `u:${u}`), b.tpl && "tpl")
                  : b.tpl ? keys(b.key === "h1" ? "tpl:h1" : null, "tpl") : undefined;
                const isHead = b.origin === "unit" && b.key.endsWith(":h");
                const tag = cmp && isHead ? [...new Set(b.unit_ids.map((u) => TAG[changes.get(u) || ""]).filter(Boolean))].join(" · ") : "";
                if (b.type === "numbered_list_item") n += 1; else n = 0;
                if (b.type === "numbered_list_item") {
                  return <div key={b.key} className="blk lt-bg" data-k={k} data-side={k ? "dst" : undefined}><div className="b-ol"><em>{n}.</em><span><Rich segs={b.text} /></span></div></div>;
                }
                return <BlockView key={b.key} b={b} k={k} tag={tag} heading={b.key === "h1" ? p.heading : undefined} fieldOf={fieldOf} />;
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
