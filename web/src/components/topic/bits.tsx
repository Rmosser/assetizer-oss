/* 专题's words and small pieces, shared by its three pages (pages/Topics, Topic, TopicVideo). */
import { useCallback, useEffect, useRef, useState } from "react";
import { groupName, resets, type Group } from "../../box";
import { useLeaving } from "../../hooks";
import { failText, retryText, VENDOR_NAME, WAIT, type Retry } from "../../util";

export type Fit = "high" | "some" | "none";
export const FIT: Record<string, string> = { high: "值得看", some: "可参考", none: "无关" };

/** A read's steps (topics/run.py STEPS) in a word, and the four stretches its track shows */
export const STEP: Record<string, string> = {
  ACQUIRE: "下载", DRAFT: "底稿", PLAN: "计划", LOOK: "回看", SECOND: "复核", WRITE: "写作", LOOK2: "补看", SECOND2: "复核", REVISE: "改稿",
};
export const PHASES = ["底稿", "回看", "写作"] as const;
export const phaseOf = (step?: string | null) =>
  !step || step === "ACQUIRE" || step === "DRAFT" ? 0 : step === "PLAN" || step === "LOOK" || step === "SECOND" ? 1 : 2;

export const TOPIC_WAIT: Record<string, string> = { ...WAIT, TOPIC_QUOTA: "等额度" };

export type Item = {
  no: number; id: string; url: string; title: string | null; channel: string | null; duration: number | null; published: string | null;
  thumb: string; drafted: boolean; media: string; availability: string | null;
  state: "QUEUED" | "RUNNING" | "WAITING" | "DONE" | "FAILED"; step: string | null;
  progress: { done?: number; of?: number; retry?: Retry } | null; wait: string | null; error_class: string | null; error: string | null;
  fit: Fit | null; gist: string | null; excluded: boolean; force: boolean; stale: boolean; done_rev: number | null;
  finished_at: string | null; added_at: string;
};
/** A subscription the topics' steps draw on that is down to the share they leave (run.held) */
export type Held = { window: string; reset_at: string | null; remaining?: number; vendor?: string | null; group?: string | null } | null;

/** 32:10 · 1:02:03 */
export function len(sec?: number | null): string {
  if (!sec) return "";
  const s = Math.round(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  const two = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${two(m)}:${two(x)}` : `${m}:${two(x)}`;
}

/** 9.5 小时 · 45 分钟 */
export function hours(h: number): string {
  if (!h) return "";
  return h < 1 ? `${Math.max(1, Math.round(h * 60))} 分钟` : `${Math.round(h * 10) / 10} 小时`;
}

/** 14:20 today, 11月8日 this year, 2025年11月8日 before */
export function when(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso), n = new Date();
  if (isNaN(+d)) return "";
  if (d.toDateString() === n.toDateString()) return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${d.getFullYear() === n.getFullYear() ? "" : `${d.getFullYear()}年`}${d.getMonth() + 1}月${d.getDate()}日`;
}

/** What holds the topics back, in words: Gemini 周额度只剩 15% · 周五 07:34 重置 */
export function heldText(h: Held): string {
  if (!h) return "";
  const who = h.group ? groupName({ name: h.group } as Group) : VENDOR_NAME[h.vendor || ""] || h.vendor || "";
  const at = resets(h.reset_at, h.window === "weekly");
  return `${who ? `${who} ` : ""}${h.window === "weekly" ? "周" : "5 小时"}额度${h.remaining != null ? `只剩 ${share(h.remaining)}` : "不够"}${at ? ` · ${at}` : ""}`;
}

/** Where a video's read stands, in a few words, and how it is said: `bad` 朱 (it needs the user), `quiet` ink-3.
 *  `stop`: why what has not started does not — its topic paused, or the quota kept for the channels (run.held). */
export function itemState(it: Item, stop?: "paused" | "held"): { text: string; tone?: "bad" | "quiet" | "live" } {
  if (it.excluded) return { text: "不算这期", tone: "quiet" };
  if (stop === "paused" && (it.state === "QUEUED" || it.state === "WAITING")) return { text: "已暂停", tone: "quiet" };
  if (stop === "held" && it.state === "QUEUED") return { text: TOPIC_WAIT.TOPIC_QUOTA, tone: "quiet" };
  const p = it.progress;
  switch (it.state) {
    case "RUNNING": {
      const r = retryText(p?.retry);
      return { text: r || `${STEP[it.step || ""] || "准备"}${p?.of ? ` ${p.done ?? 0}/${p.of}` : ""}`, tone: "live" };
    }
    case "QUEUED": return { text: it.stale ? "按新问题重读" : "排队", tone: "quiet" };
    case "WAITING": return { text: TOPIC_WAIT[it.wait || ""] || "在等", tone: "quiet" };
    case "FAILED": return { text: `没读成 · ${failText(it.error_class)}`, tone: "bad" };
    default: return { text: FIT[it.fit || "some"], tone: it.fit === "none" ? "quiet" : undefined };
  }
}

/** The batch at a glance: the videos read by what they have for the question, then those under way, waiting, failed. */
export function Mix({ c }: { c: Partial<Record<"high" | "some" | "none" | "running" | "waiting" | "queued" | "failed", number>> }) {
  const parts: [string, number][] = [["high", c.high ?? 0], ["some", c.some ?? 0], ["none", c.none ?? 0], ["running", c.running ?? 0],
    ["waiting", c.waiting ?? 0], ["queued", c.queued ?? 0], ["failed", c.failed ?? 0]];
  const all = parts.reduce((n, [, k]) => n + k, 0);
  if (!all) return null;
  return (
    <span className="tp-mix" aria-hidden>
      {parts.filter(([, k]) => k > 0).map(([k, n]) => <i key={k} className={k} style={{ flexGrow: n }} />)}
    </span>
  );
}

/** A read's three stretches — 底稿, 回看, 写作 — the one under way breathing */
export function Phases({ it }: { it: Item }) {
  const at = it.state === "DONE" ? 3 : phaseOf(it.step);
  const live = it.state === "RUNNING";
  return (
    <span className="tp-ph3" aria-hidden>
      {PHASES.map((p, i) => <i key={p} className={i < at ? "done" : i === at && live ? "run" : ""} />)}
    </span>
  );
}

/** Up to three thumbnails stacked like a deck: a topic's videos */
export function Deck({ thumbs }: { thumbs: string[] }) {
  return (
    <span className="tp-deck" aria-hidden>
      {thumbs.slice(0, 3).map((t, i) => <img key={t} src={t} alt="" loading="lazy" style={{ zIndex: 3 - i }} onError={(e) => { e.currentTarget.style.visibility = "hidden"; }} />)}
    </span>
  );
}

/** A YouTube thumbnail that does not show a broken image when it is gone */
export function Thumb({ src, className = "" }: { src: string; className?: string }) {
  return (
    <span className={`tp-th ${className}`} aria-hidden>
      <img src={src} alt="" loading="lazy" onError={(e) => { e.currentTarget.style.visibility = "hidden"; }} />
    </span>
  );
}

/** A video's thumbnail at YouTube, by size: mq 320×180, hq 480×360 */
export const thumbOf = (id: string, size: "mq" | "hq" = "mq") => `https://i.ytimg.com/vi/${id}/${size}default.jpg`;

/** A step that cannot be taken back: the first click arms it, the second takes it (four seconds to think again) */
export function Armed({ label, sure, onGo }: { label: string; sure: string; onGo: () => void }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button type="button" className={`btn sm ${armed ? "armed" : "ghost"}`} onBlur={() => setArmed(false)}
      onClick={() => { if (armed) onGo(); else setArmed(true); }}>{armed ? sure : label}</button>
  );
}

export type Said = { text: string; bad?: boolean };

/** How an action went, for a moment at the foot of the page */
export function useToast() {
  const [said, setSaid] = useState<Said | null>(null);
  const told = useLeaving(said);
  const timer = useRef<number | undefined>(undefined);
  const say = useCallback((s: Said) => {
    setSaid(s);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setSaid(null), s.bad ? 6000 : 2200);
  }, []);
  useEffect(() => () => clearTimeout(timer.current), []);
  const toast = (
    <div role={told.shown?.bad ? "alert" : "status"}>
      {told.shown && <div className={`ch-toast${told.shown.bad ? " bad" : ""}${told.leaving ? " leaving" : ""}`}>{told.shown.text}</div>}
    </div>
  );
  return { say, toast };
}

/** 0.8% · 12%: a share of a quota, a tenth of a point below ten */
export const share = (x: number) => (x < 0.095 ? `${(Math.round(x * 1000) / 10).toFixed(1).replace(/\.0$/, "")}%` : `${Math.round(x * 100)}%`);
