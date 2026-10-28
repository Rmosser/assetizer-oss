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
  squote: new RegExp(`(?<=[${HAN}])\\s*[‘']([^‘’'\\n]{1,40})[’']\\s*(?=[${HAN}])`, "gu"),
  hanLatin: new RegExp(`([${HAN}])([A-Za-z0-9$])`, "gu"),
  latinHan: new RegExp(`([A-Za-z0-9%])([${HAN}])`, "gu"),
};

/** Half-width punctuation inside Chinese text → full-width; straight quotes → curly. */
export function cjkPunct(s: string): string {
  return (s || "")
    .replace(RE.comma, "，").replace(RE.semi, "；").replace(RE.colon, "：")
    .replace(RE.ques, "？").replace(RE.bang, "！")
    .replace(RE.paren, "（$1）").replace(RE.dquote, "“$1”").replace(RE.squote, "“$1”");
}

/** A thin space between Han and Latin/digits (what `text-autospace` does where the browser supports it). */
export function autospace(s: string): string {
  return (s || "").replace(RE.hanLatin, "$1 $2").replace(RE.latinHan, "$1 $2");
}

/* Latin/Han spacing is left to CSS `text-autospace` (it knows about line edges; a literal thin space does not). */
export const typeset = (s: string) => cjkPunct(s);


/* Break opportunities only at phrase joints — before 与/及/并, after 的、：；，, around brackets and quotes.
   Everything else stays together (keep-all); Segmenter splits finance terms like 收|敛. No joint between a
   ticker and Han: a <wbr> there would cancel `text-autospace`.
   Han–Latin boundaries stay inside one text node, so `text-autospace` still adds its 1/8 em. */
const JOINT = new RegExp(`(?=[与及并](?=[${HAN}A-Za-z0-9]))|(?<=[的、：；，])|(?=[（「“《])|(?<=[）」”》])`, "gu");

export function Words({ children }: { children: string }) {
  // " · " between a title's parts: the dot stays with what is before it, so no line starts with it
  const runs = typeset(children || "").replace(/ +· +/g, " · ").split(JOINT).filter(Boolean);
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
/** Chapter-style ordinal: 3 → 〇三, 10 → 十, 35 → 三十五 (a lone 一 reads as a dash) */
export function cnNo(n: number): string {
  if (n < 0 || n > 99) return String(n);
  if (n <= 10) return n === 10 ? "十" : "〇" + CN[n];
  const t = Math.floor(n / 10), o = n % 10;
  return (t > 1 ? CN[t] : "") + "十" + (o ? CN[o] : "");
}

/** Quoted fragments inside a note: “…” or 「…」 (used to find the sentence a 校注 is about). */
export function quotedFragments(s: string): string[] {
  const out: string[] = [];
  for (const m of (s || "").matchAll(/[“「]([^”」]{4,80})[”」]/g)) out.push(m[1]);
  return out;
}

/** Internal vocabulary that leaks from model notes into reader-facing text. */
export const deJargon = (s: string) => (s || "")
  .replace(/Claim\s*\d+\s*/g, "正文")
  .replace(/ASR\s*原文/g, "原话转写").replace(/\bASR\b/g, "转写")
  .replace(/被禁用的夸大词汇/g, "夸大措辞")
  .replace(/（[A-Z_]{3,}）/g, "");

/** Close a sentence that the model left open. */
export const endStop = (s: string) => (/[\p{Script=Han}」”）A-Za-z0-9]$/u.test((s || "").trim()) ? `${s.trim()}。` : s);

/** The issue's headline: the pipeline's 题眼, else the first must-read unit, else the cleaned video title. */
export const issueHeadline = (headline: string | null | undefined, mustTitle: string | null | undefined, title: string) =>
  (headline || mustTitle || splitTitle(title).head).trim();

/** Original title as a quiet second line: 技术分析视频 · 持仓、板块个股分析… */
export const originalTitle = (title: string) => {
  const { head, dek } = splitTitle(title);
  return dek ? `${head} · ${dek}` : head;
};
