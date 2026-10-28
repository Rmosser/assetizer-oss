import { useEffect, useRef, useState } from "react";
import { Fold } from "../Fold";
import { ymd } from "../Figures";
import { Icon } from "./bits";

/* 补录's days: one pill that says the range and unfolds a month under it. The first day picked starts a range, the
   second ends it (in either order) and folds the month away; until then the band follows the pointer. Days after today
   cannot be picked; an empty start means from the channel's first video. */

const WEEK = ["一", "二", "三", "四", "五", "六", "日"];
const at = (s: string) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const shift = (s: string, days: number, months = 0) => {
  const d = at(s);
  if (months) {
    const last = new Date(d.getFullYear(), d.getMonth() + months + 1, 0).getDate();
    return ymd(new Date(d.getFullYear(), d.getMonth() + months, Math.min(d.getDate(), last)));
  }
  d.setDate(d.getDate() + days);
  return ymd(d);
};
const month = (ym: string, n: number) => ymd(new Date(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1)).slice(0, 7);
const name = (s: string, today: string) => {
  if (s === today) return "今天";
  const d = at(s);
  return `${s.slice(0, 4) === today.slice(0, 4) ? "" : `${d.getFullYear()}年`}${d.getMonth() + 1}月${d.getDate()}日`;
};

export function Days({ since, until, on, onChange }: {
  since: string; until: string; on: boolean; onChange: (since: string, until: string) => void;
}) {
  const today = ymd(new Date());
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [view, setView] = useState((until || today).slice(0, 7));
  const [months, setMonths] = useState(false);  // the year's twelve months, to go back far in one step
  const [focus, setFocus] = useState(until || today);
  const pill = useRef<HTMLButtonElement>(null);
  const grid = useRef<HTMLDivElement>(null);
  const moved = useRef(false);
  useEffect(() => { setAnchor(null); }, [since, until]);
  useEffect(() => {
    if (!moved.current) return;
    moved.current = false;
    grid.current?.querySelector<HTMLButtonElement>(`[data-day="${focus}"]`)?.focus();
  }, [focus, view, months]);

  const close = () => { setOpen(false); setAnchor(null); setMonths(false); };
  const toggle = () => {
    if (open) { close(); return; }
    const d = until || today;
    setView(d.slice(0, 7));
    setFocus(d);
    setOpen(true);
    moved.current = true;
  };
  const go = (d: string) => {
    const c = d > today ? today : d;
    setFocus(c);
    setView(c.slice(0, 7));
    moved.current = true;
  };
  const pick = (d: string) => {
    if (!anchor) { setAnchor(d); setHover(d); setFocus(d); return; }
    const [a, b] = anchor <= d ? [anchor, d] : [d, anchor];
    close();
    onChange(a, b);
    pill.current?.focus();
  };
  const key = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();  // the sheet's <dialog> would close on it
      e.stopPropagation();
      if (anchor) setAnchor(null);
      else { close(); pill.current?.focus(); }
      return;
    }
    if (months || !(e.target as HTMLElement).dataset.day) return;
    const step: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [-7, 0], ArrowDown: [7, 0], PageUp: [0, -1], PageDown: [0, 1] };
    const s = step[e.key];
    if (!s) return;
    e.preventDefault();
    const d = shift(focus, s[0], s[1]);
    go(d);
    if (anchor) setHover(d > today ? today : d);
  };

  // the band: the range being picked (anchor to the pointer), else the one in use
  const end = anchor ? (hover || anchor) : null;
  const [lo, hi] = anchor ? (anchor <= end! ? [anchor, end!] : [end!, anchor]) : [since, until];
  const first = at(`${view}-01`);
  const count = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  const lead = (first.getDay() + 6) % 7;
  const thisMonth = today.slice(0, 7);
  const year = view.slice(0, 4);
  const tab = focus.slice(0, 7) === view ? focus : `${view}-01`;  // the one day of the month that Tab lands on
  const text = anchor ? `${name(anchor, today)} – …` : since === until ? name(until, today)
    : `${since ? name(since, today) : "最早"} – ${name(until, today)}`;

  return (
    <div className="ch-days" onKeyDown={key}>
      <button ref={pill} type="button" className={`ch-pkt${on ? " on" : ""}`} aria-expanded={open} aria-label={`日期范围 ${text}`} onClick={toggle}>
        <Icon name="date" size={14} />
        <span>{text}</span>
        <Icon name="chevd" size={14} />
      </button>
      <Fold open={open}>
        <div className="ch-cal" ref={grid}>
          <div className="ch-calh">
            <button type="button" className="ch-calt" aria-expanded={months} onClick={() => setMonths((m) => !m)}>
              {months ? `${year}年` : `${year}年${+view.slice(5)}月`}<Icon name="chevd" size={14} />
            </button>
            <button type="button" className="ch-icb ch-calp" aria-label={months ? "上一年" : "上个月"}
              onClick={() => setView((v) => month(v, months ? -12 : -1))}><Icon name="chev" /></button>
            <button type="button" className="ch-icb" aria-label={months ? "下一年" : "下个月"}
              disabled={months ? year >= thisMonth.slice(0, 4) : view >= thisMonth}
              onClick={() => setView((v) => { const n = month(v, months ? 12 : 1); return n > thisMonth ? thisMonth : n; })}><Icon name="chev" /></button>
          </div>
          {months ? (
            <div className="ch-calm">
              {Array.from({ length: 12 }, (_, i) => {
                const ym = `${year}-${String(i + 1).padStart(2, "0")}`;
                const inside = (!lo || ym >= lo.slice(0, 7)) && !!hi && ym <= hi.slice(0, 7);
                return (
                  <button key={ym} type="button" disabled={ym > thisMonth} className={`${ym === view ? "on" : ""}${inside ? " in" : ""}`}
                    onClick={() => { setView(ym); setMonths(false); }}>{i + 1}月</button>
                );
              })}
            </div>
          ) : (
            <>
              <div className="ch-calw" aria-hidden>{WEEK.map((w) => <span key={w}>{w}</span>)}</div>
              <div className={`ch-cald${anchor ? " pv" : ""}`} role="grid" aria-label={`${year}年${+view.slice(5)}月`}
                onMouseLeave={() => anchor && setHover(anchor)}>
                {Array.from({ length: lead }, (_, i) => <span key={`_${i}`} />)}
                {Array.from({ length: count }, (_, i) => {
                  const d = `${view}-${String(i + 1).padStart(2, "0")}`;
                  const col = (lead + i) % 7;
                  const inside = (!lo || d >= lo) && !!hi && d <= hi;
                  const cls = [inside && "in", d === lo && "lo", d === hi && "hi", d === today && "now",
                    inside && (col === 0 ? "e0" : i === 0 && "c0"), inside && (col === 6 ? "e1" : i === count - 1 && "c1")].filter(Boolean).join(" ");
                  return (
                    <button key={d} type="button" data-day={d} className={cls} disabled={d > today} tabIndex={d === tab ? 0 : -1}
                      aria-pressed={d === lo || d === hi} aria-label={`${+view.slice(5)}月${i + 1}日`}
                      onMouseEnter={() => anchor && setHover(d)} onFocus={() => setFocus(d)} onClick={() => pick(d)}>{i + 1}</button>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </Fold>
    </div>
  );
}
