/* 专题's notes and answers cite their sources inline (topics/prompts): `[12:34 说]` what was said, `[12:34.5 帧]` a frame,
   `[02:54 画面]` the picture, `[L03]` `[R01]` a look; the answer puts the video in front, `[V3 12:34 说]`, `[V3 L03]`, `[V3]`.
   Each becomes a button the page answers (the player, the frame, the look, the video), and the words the notes use for what
   is not settled are marked. */
import DOMPurify from "dompurify";
import { marked } from "marked";
import { typeset } from "../../cjk";

export type Cite = { v?: number; t?: number; at?: string; kind?: "say" | "frame"; look?: string };

const TIME = String.raw`(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?`;
const PART = new RegExp(String.raw`\s*(?:V(\d{1,3})\s*)?(?:(${TIME})(?:\s*[-–—~至到]\s*${TIME})?\s*(说|帧|画面)?|([LR]\d{1,3}))?\s*(?:[,，;；、/]\s*)?`, "y");

/** 12:34.5 → 754.5; 1:02:03 → 3723 */
export function secs(at: string): number {
  return at.split(":").reduce((s, p) => s * 60 + parseFloat(p), 0);
}

/** What a bracket cites, or null when it is something else ([标题], a link's text). A time or look without a video takes the
 *  video before it: [V3 12:34 说，13:02 说]. */
export function parseCites(inner: string): Cite[] | null {
  const out: Cite[] = [];
  let at = 0, v: number | undefined;
  PART.lastIndex = 0;
  while (at < inner.length) {
    PART.lastIndex = at;
    const m = PART.exec(inner);
    if (!m || m[0].length === 0) return null;
    at = PART.lastIndex;
    const [, no, time, kind, look] = m;
    if (!no && !time && !look) continue;  // a separator
    if (no) v = +no;
    if (time) out.push({ v, t: secs(time), at: time, kind: kind === "说" || !kind ? "say" : "frame" });
    else if (look) out.push({ v, look });
    else out.push({ v });
  }
  return out.length ? out : null;
}

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));

const FRAME = '<svg viewBox="0 0 12 12" width="9" height="9" aria-hidden="true"><rect x="1.2" y="2.2" width="9.6" height="7.6" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M2.6 8.4 5 6l1.6 1.5 1.3-1.2 1.6 1.9" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/></svg>';
const LOOK = '<svg viewBox="0 0 12 12" width="9" height="9" aria-hidden="true"><circle cx="5.2" cy="5.2" r="3.4" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="m7.8 7.8 2.6 2.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';

/** A citation's words for a tooltip and a screen reader: 12:34 原话 · 12:34.5 画面 · 回看 L03 */
export function citeWords(c: Cite): string {
  if (c.look) return `回看 ${c.look}`;
  if (c.at) return `${c.at} ${c.kind === "frame" ? "画面" : "原话"}`;
  return "这期";
}

/** What a page does with its chips: what a chip says before its time (`name`: the video, on the answer), and where it goes
 *  (`href`: a link, opened with a click or in a new tab; without one it is a button the page answers) */
export type ChipWays = { name?: (c: Cite) => string | undefined; href?: (c: Cite) => string | undefined };

/** One citation as a chip. `cont`: it follows a chip of the same video, so the two read as one: V3 00:23 · 03:09 */
function chip(c: Cite, ways: ChipWays, cont = false): string {
  const said = [ways.name?.(c), citeWords(c)].filter(Boolean).join(" · ");
  const data = [c.v != null && `data-v="${c.v}"`, c.t != null && `data-t="${c.t}"`, c.at && `data-at="${esc(c.at)}"`,
    c.kind && `data-k="${c.kind}"`, c.look && `data-l="${esc(c.look)}"`].filter(Boolean).join(" ");
  const v = c.v != null && !cont ? `<b>V${c.v}</b>` : "";
  const body = c.look ? `${LOOK}<span>${esc(c.look)}</span>` : c.at ? `<span class="num">${esc(c.at)}</span>${c.kind === "frame" ? FRAME : ""}` : "";
  const cls = `tp-c${c.look ? " look" : c.kind === "frame" ? " frame" : ""}${cont ? " cont" : ""}`;
  const href = ways.href?.(c);
  return href
    ? `<a class="${cls}" href="${esc(href)}" ${data} title="${esc(said)}" aria-label="${esc(said)}">${v}${body}</a>`
    : `<button type="button" class="${cls}" ${data} title="${esc(said)}" aria-label="${esc(said)}">${v}${body}</button>`;
}

/** The citations of brackets side by side, as chips: those of one video after another go together */
function chips(cs: Cite[], ways: ChipWays): string {
  return cs.map((c, i) => chip(c, ways, i > 0 && c.v != null && c.v === cs[i - 1].v && (c.t != null || !!c.look))).join("");
}

/** The citation a chip stands for, from its data-* (the page's one click handler reads it). */
export function citeOf(el: Element | null): Cite | null {
  const b = el?.closest<HTMLElement>(".tp-c");
  if (!b) return null;
  const d = b.dataset;
  return { v: d.v ? +d.v : undefined, t: d.t ? +d.t : undefined, at: d.at, kind: d.k as Cite["kind"], look: d.l };
}

const MARKS: [RegExp, string][] = [[/两个模型说法不同/g, "tp-split"], [/画面看不清/g, "tp-unsure"], [/未核实/g, "tp-unsure"]];
// brackets next to each other ([V3 00:23 说][V3 03:09 说]) are one group, and the punctuation after it goes with it:
// the browser breaks after an inline button even before a word joiner, and a line would start with the comma
const RUN = /\uE000\d+\uE001(?:\s*\uE000\d+\uE001)*([\uFF0C\u3002\u3001\uFF1B\uFF1A\uFF01\uFF1F\uFF09\u300D\u300F\u300B\u201D\u2019,.;:!?)])?/g;

/** Markdown with its citations as chips. The brackets are marked before the markdown is read and become chips after it
 *  is sanitized, in text only: a mark that lands in an attribute (an image's alt) is given back the words it stood for. */
export function renderCited(src: string, ways: ChipWays = {}): string {
  const found: Cite[][] = [], said: string[] = [];
  const pre = (src || "").replace(/[\uE000\uE001]/g, "").replace(/(!?)\[([^[\]\n]{1,160})\](?!\()/g, (m, bang: string, inner: string) => {
    const cs = bang ? null : parseCites(inner);
    if (!cs) return m;
    found.push(cs);
    said.push(m);
    return `\uE000${found.length - 1}\uE001`;
  });
  const doc = document.createElement("template");
  doc.innerHTML = DOMPurify.sanitize(marked.parse(typeset(pre), { async: false, gfm: true, breaks: false }) as string);
  const back = (s: string) => s.replace(/\uE000(\d+)\uE001/g, (_, n: string) => said[+n] ?? "");
  for (const el of doc.content.querySelectorAll("*")) {
    for (const a of [...el.attributes]) if (a.value.includes("\uE000")) el.setAttribute(a.name, back(a.value));
  }
  const walk = document.createTreeWalker(doc.content, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  while (walk.nextNode()) texts.push(walk.currentNode as Text);
  for (const t of texts) {
    if (!t.data.includes("\uE000") && !MARKS.some(([re]) => t.data.search(re) >= 0)) continue;
    let h = esc(t.data);
    for (const [re, cls] of MARKS) h = h.replace(re, `<mark class="${cls}">$&</mark>`);
    h = h.replace(RUN, (run, p = "") =>
      `<span class="tp-cs">${chips([...run.matchAll(/\uE000(\d+)\uE001/g)].flatMap((m) => found[+m[1]] ?? []), ways)}${p}</span>`);
    const piece = document.createElement("template");
    piece.innerHTML = h;
    t.replaceWith(piece.content);
  }
  return doc.innerHTML;
}

/** One line of model text with its citations taken out (a list's summary line). */
export const uncited = (s: string) =>
  typeset((s || "").replace(/\s*\[([^[\]\n]{1,160})\](?!\()/g, (m, inner: string) => (parseCites(inner) ? "" : m)).replace(/[#*_`>]+/g, "").trim());
