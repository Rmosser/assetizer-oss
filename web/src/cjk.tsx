/* Chinese typesetting helpers: punctuation normalisation, Latin/Han spacing, word-aware line breaking,
   and the clean-up of raw YouTube / LLM titles before they reach the page. */
import { Fragment } from "react";

const HAN = "\\p{Script=Han}";
const RE = {
  comma: new RegExp(`(?<=[${HAN}])\\s*,\\s*|,\\s*(?=[${HAN}])`, "gu"),
  semi: new RegExp(`(?<=[${HAN}])\\s*;\\s*|;\\s*(?=[${HAN}])`, "gu"),
  colon: new RegExp(`(?<=[${HAN}])\\s*:\\s*(?![/\\d])`, "gu"),
  ques: new RegExp(`(?<=[${HAN}])\\?`, "gu"),
  bang: new RegExp(`(?<=[${HAN}])!(?!\\[)`, "gu"),
  paren: new RegExp(`\\(([^()\\n]*[${HAN}][^()\\n]*)\\)`, "gu"),
  dquote: new RegExp(`"([^"\\n]*[${HAN}][^"\\n]*)"`, "gu"),
  hanLatin: new RegExp(`([${HAN}])([A-Za-z0-9$])`, "gu"),
  latinHan: new RegExp(`([A-Za-z0-9%])([${HAN}])`, "gu"),
};

/** Half-width punctuation inside Chinese text → full-width; straight quotes → curly. */
export function cjkPunct(s: string): string {
  return (s || "")
    .replace(RE.comma, "，").replace(RE.semi, "；").replace(RE.colon, "：")
    .replace(RE.ques, "？").replace(RE.bang, "！")
    .replace(RE.paren, "（$1）").replace(RE.dquote, "“$1”");
}

/** A thin space between Han and Latin/digits (what `text-autospace` does where the browser supports it). */
export function autospace(s: string): string {
  return (s || "").replace(RE.hanLatin, "$1 $2").replace(RE.latinHan, "$1 $2");
}

/* Latin/Han spacing is left to CSS `text-autospace` (it knows about line edges; a literal thin space does not). */
export const typeset = (s: string) => cjkPunct(s);

const seg = typeof Intl !== "undefined" && "Segmenter" in Intl ? new Intl.Segmenter("zh-Hans", { granularity: "word" }) : null;

const HAN_END = new RegExp(`[${HAN}]$`, "u");
const HAN_START = new RegExp(`^[${HAN}]`, "u");

/** Break lines only between words: keep-all, plus a <wbr> at each Han–Han word boundary. Han–Latin boundaries
 *  stay inside one text node so `text-autospace` can still put its 1/8 em there. */
export function Words({ children }: { children: string }) {
  const text = typeset(children || "");
  if (!seg) return <>{text}</>;
  const runs: string[] = [];
  let buf = "";
  for (const { segment } of seg.segment(text)) {
    if (buf && HAN_END.test(buf) && HAN_START.test(segment)) { runs.push(buf); buf = ""; }
    buf += segment;
  }
  if (buf) runs.push(buf);
  return <span className="kw">{runs.map((r, i) => <Fragment key={i}>{i > 0 && <wbr />}{r}</Fragment>)}</span>;
}

/** "09/08/2026 技术分析视频 持仓/板块个股分析/关注列表" → { head: "技术分析视频", dek: "持仓、板块个股分析、关注列表" } */
export function splitTitle(raw: string): { head: string; dek: string } {
  const t = (raw || "").replace(/^\s*(\d{1,2}\/\d{1,2}\/\d{2,4}|\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{8})\s*/, "").trim();
  const list = (x: string) => x.split(/\s*[/|｜]\s*/).map((s) => s.trim()).filter(Boolean);
  const m = t.match(/^(\S.*?)\s+(\S*[/|｜].*)$/);
  if (m) return { head: m[1], dek: list(m[2]).join("、") };
  if (/[/|｜]/.test(t)) return { head: list(t).join("、"), dek: "" };
  return { head: t || raw, dek: "" };
}

/** inline form for lists: 技术分析视频：持仓、板块个股分析、关注列表 */
export function IssueTitle({ title }: { title: string }) {
  const { head, dek } = splitTitle(title);
  return <Words>{dek ? `${head}：${dek}` : head}</Words>;
}

export const plainIssueTitle = (title: string) => {
  const { head, dek } = splitTitle(title);
  return dek ? `${head}：${dek}` : head;
};

/** "03. MRNA 双重缺口（MRNA）" → "MRNA 双重缺口": drop LLM numbering and a trailing ticker list already shown as tags. */
export function cleanUnitTitle(h: string): string {
  return (h || "")
    .replace(/^\s*\d{1,3}\s*[.、．]\s*/, "")
    .replace(/\s*[（(]\s*[A-Z0-9.\-]{1,8}(\s*[,，、/]\s*[A-Z0-9.\-]{1,8})*\s*[)）]\s*$/, "")
    .trim();
}

const CN = "〇一二三四五六七八九";
/** Chapter-style ordinal: 3 → 三, 10 → 十, 35 → 三十五 */
export function cnNo(n: number): string {
  if (n < 0 || n > 99) return String(n);
  if (n <= 10) return n === 10 ? "十" : CN[n];
  const t = Math.floor(n / 10), o = n % 10;
  return (t > 1 ? CN[t] : "") + "十" + (o ? CN[o] : "");
}

/** Quoted fragments inside a note: “…” or 「…」 (used to find the sentence a 朱批 is about). */
export function quotedFragments(s: string): string[] {
  const out: string[] = [];
  for (const m of (s || "").matchAll(/[“「]([^”」]{4,80})[”」]/g)) out.push(m[1]);
  return out;
}
