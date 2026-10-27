/** Small building blocks of the channel page: formatting, icons, segmented controls, unit tracks. */
import { useSyncExternalStore } from "react";

export function useMedia(query: string) {
  return useSyncExternalStore((on) => {
    const q = matchMedia(query);
    q.addEventListener("change", on);
    return () => q.removeEventListener("change", on);
  }, () => matchMedia(query).matches);
}
/** Phones and narrow windows get the one-column layout. */
export const useNarrow = () => useMedia("(max-width: 860px)");
/** Below this the source and the Notion page share one column and take turns. */
export const useCompact = () => useMedia("(max-width: 1399px)");

export const fmt = (t?: number | null) => {
  const s = Math.max(0, Math.round(t || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(x)}` : `${m}:${p(x)}`;
};
export const est = (minutes: number) =>
  minutes >= 90 ? `约 ${Math.round(minutes / 30) / 2} 小时` : `约 ${Math.max(1, Math.round(minutes))} 分钟`;
export const day = (s?: string | null) => (s && /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : (s || "").slice(0, 10));
export const cnDate = (s?: string | null) => {
  const d = day(s);
  if (!d) return "";
  const [y, m, dd] = d.split("-");
  return `${y}年${Number(m)}月${Number(dd)}日`;
};
export const hm = (iso?: string | null, addMinutes = 0) => {
  if (!iso) return "";
  const d = new Date(new Date(iso).getTime() + addMinutes * 60000);
  return Number.isNaN(+d) ? "" : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
const CN = "零一二三四五六七八九十";
export const cn = (n: number) => (n <= 10 ? CN[n] : n < 20 ? "十" + (n > 10 ? CN[n - 10] : "") : CN[Math.floor(n / 10)] + "十" + (n % 10 ? CN[n % 10] : ""));
export const short = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

type IconName = "date" | "text" | "relation" | "select" | "time" | "url" | "title" | "number" | "check" | "chev" | "chevd" | "play"
  | "x" | "pen" | "plus" | "db" | "lock" | "swap" | "box" | "ext";
const PATHS: Record<IconName, React.ReactNode> = {
  date: <><rect x="3.5" y="5" width="17" height="15" rx="2" /><path d="M3.5 9.5h17M8 3v4M16 3v4" /></>,
  text: <path d="M4 7h16M4 12h16M4 17h10" />,
  relation: <path d="M7 17L17 7M9 7h8v8" />,
  select: <><circle cx="12" cy="12" r="8.5" /><path d="M8.5 10.5l3.5 3.5 3.5-3.5" /></>,
  time: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  url: <><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" /><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" /></>,
  title: <path d="M5 6h14M12 6v13" />,
  number: <path d="M9 4L7 20M17 4l-2 16M4 9h16M3 15h16" />,
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  chev: <path d="M9 6l6 6-6 6" />,
  chevd: <path d="M7 10l5 5 5-5" />,
  play: <path d="M7 4l13 8-13 8z" />,
  x: <path d="M6 6l12 12M18 6L6 18" />,
  pen: <><path d="M4 20h4L19 9l-4-4L4 16z" /><path d="M13.5 6.5l4 4" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  db: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 10h18M9 10v10" /></>,
  lock: <><rect x="4" y="4" width="16" height="16" rx="2.5" /><path d="M8.5 12.5l2.5 2.5 4.5-6" /></>,
  swap: <path d="M7 7h11l-3-3M17 17H6l3 3" />,
  box: <rect x="5" y="5" width="14" height="14" rx="2" />,
  ext: <path d="M14 5h5v5M19 5l-8 8M10 6H6a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h11a1 1 0 0 0 1-1v-4" />,
};
export function Icon({ name, size = 15, fill = false }: { name: IconName; size?: number; fill?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={fill ? "currentColor" : "none"} stroke={fill ? "none" : "currentColor"}
      strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden className="ico">{PATHS[name]}</svg>
  );
}
export const TYPE_ICON: Record<string, IconName> = {
  title: "title", rich_text: "text", select: "select", multi_select: "select", status: "select", date: "date", number: "number",
  checkbox: "check", url: "url", relation: "relation", created_time: "time", last_edited_time: "time",
};

export function Seg<T extends string>({ value, options, onChange, label, small }: {
  value: T; options: { value: T; label: React.ReactNode; title?: string; disabled?: boolean }[]; onChange: (v: T) => void; label: string; small?: boolean;
}) {
  return (
    <div className={`ch-seg${small ? " sm" : ""}`} role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={o.value === value} className={o.value === value ? "on" : ""}
          title={o.title} disabled={o.disabled} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function Switch({ on, onChange, label, disabled }: { on: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled} className={`ch-switch${on ? " on" : ""}`} onClick={() => onChange(!on)} />;
}

export function Stepper({ value, onChange, step = 5, min = 0, max = 240, label }: {
  value: number; onChange: (v: number) => void; step?: number; min?: number; max?: number; label: string;
}) {
  return (
    <span className="ch-step" role="group" aria-label={label}>
      <button type="button" aria-label="减少" onClick={() => onChange(Math.max(min, value - step))}>−</button>
      <b>{value}</b>
      <button type="button" aria-label="增加" onClick={() => onChange(Math.min(max, value + step))}>+</button>
    </span>
  );
}

/** ✓ in a ring: this run is the issue's version. Filled = pinned by you (a newer run won't replace it). */
export function Adopt({ pinned, small }: { pinned: boolean; small?: boolean }) {
  return (
    <span className={`ch-ad${pinned ? " pin" : ""}${small ? " sm" : ""}`}
      title={pinned ? "你选用的版本，之后跑完的版本不会替换它" : "最新完成的一版，之后跑完会被替换"}>
      <Icon name="check" size={small ? 8 : 9} />
    </span>
  );
}

/** A run's knowledge units on the video's time line; red ticks are 校注. */
export function Track({ units, flags, dur, keyOf, mark, onPick, tall }: {
  units: [number, number, number][]; flags: number[]; dur: number; keyOf?: (i: number) => string; mark?: (i: number) => string;
  onPick?: (i: number) => void; tall?: boolean;
}) {
  const d = Math.max(1, dur || (units.length ? units[units.length - 1][1] : 1));
  return (
    <div className={`ch-trk${tall ? " tall" : ""}`} aria-hidden={!onPick}>
      {units.map(([a, b, p], i) => (
        <i key={i} className={`lt-seg${i % 2 ? " z" : ""}${p === 3 ? " m" : ""} ${mark ? mark(i) : ""}`} data-k={keyOf?.(i)} data-side="src"
          style={{ left: `${(a / d) * 100}%`, width: `${Math.max(0.5, ((b - a) / d) * 100 - 0.3)}%` }}
          onClick={onPick ? () => onPick(i) : undefined} title={onPick ? fmt(a) : undefined} />
      ))}
      {flags.map((t, i) => <b key={`f${i}`} className="fl" style={{ left: `${(t / d) * 100}%` }} />)}
    </div>
  );
}

export function Toast({ text, seal, leaving }: { text: string; seal?: boolean; leaving?: boolean }) {
  return <div className={`ch-toast${leaving ? " leaving" : ""}`} role="status">{seal && <span className="zs" aria-hidden>定</span>}<span>{text}</span></div>;
}
