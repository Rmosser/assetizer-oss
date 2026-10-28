/* Numbers and small charts shared by 刊, 机房 and the 频道 看板. One look everywhere: figures in the sans face with
   proportional digits, thin ink marks on paper, vermilion only for failures and what stops production, a tooltip on
   hover that the keyboard reaches too (one tab stop per chart, arrow keys inside). */
import { useId, useState } from "react";

const nf = new Intl.NumberFormat("zh-CN");
/** 1486 → 1,486; 103200000 → 1.03 亿; 41200000 → 4,120 万 */
export function big(n?: number | null): string {
  if (n == null || Number.isNaN(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e8) return `${(n / 1e8).toFixed(a >= 1e9 ? 1 : 2)} 亿`;
  if (a >= 1e5) return `${nf.format(Math.round(n / 1e4))} 万`;
  return nf.format(Math.round(n));
}
/** hours with one decimal under 10, whole above */
export const hrs = (seconds?: number | null) => {
  const h = (seconds || 0) / 3600;
  return h >= 10 ? String(Math.round(h)) : h.toFixed(1).replace(/\.0$/, "");
};
export const pct = (x?: number | null) => (x == null ? "—" : `${Math.round(x * 100)}%`);
/** time still to go: "约 25 分钟" under an hour and a half, then by half hours ("约 2.5 小时"), whole hours from ten
 *  on, days from two — the same on every page */
export const about = (sec: number) => {
  const m = sec / 60;
  if (m < 90) return `约 ${m < 10 ? Math.max(1, Math.round(m)) : Math.round(m / 5) * 5} 分钟`;
  if (m < 2880) return `约 ${m < 600 ? Math.round(sec / 1800) / 2 : Math.round(m / 60)} 小时`;
  return `约 ${Math.round(m / 1440)} 天`;
};

/** A stat tile: label, value (with its unit), and a line under it (a change, a share, a note). */
export function Kpi({ label, value, unit, sub, tone }: {
  label: string; value: React.ReactNode; unit?: string; sub?: React.ReactNode; tone?: "zhu" | "quiet";
}) {
  return (
    <div className={`kpi${tone ? ` ${tone}` : ""}`}>
      <span className="kpi-l">{label}</span>
      <span className="kpi-v">{value}{unit && <small>{unit}</small>}</span>
      {sub != null && sub !== false && <span className="kpi-s">{sub}</span>}
    </div>
  );
}

/** How much of a limit is left: the bar is what remains; low turns amber, nothing left turns vermilion
 *  (when it stops production), a window that does not apply is hatched, a reading that may no longer hold greyed. */
export function Meter({ label, left, tone, right, sub }: {
  label: string; left: number | null; tone: "ok" | "low" | "out" | "off" | "old"; right?: React.ReactNode; sub?: React.ReactNode;
}) {
  const v = left == null ? null : Math.round(left * 100);
  return (
    <div className={`meter ${tone}`}>
      <div className="meter-h"><span>{label}</span><b>{right ?? (v == null ? "—" : `${v}%`)}</b></div>
      <div className="meter-t" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={v ?? undefined}
        aria-valuetext={v == null ? "不适用" : `剩 ${v}%`}>
        {tone !== "off" && v != null && <i style={{ width: `${Math.max(v, v > 0 ? 1.5 : 0)}%` }} />}
      </div>
      {sub && <div className="meter-s">{sub}</div>}
    </div>
  );
}

/** Keyboard for a chart drawn as one tab stop: ←/→ (and ↑/↓ where given) move the highlighted mark, starting from
 *  and never past `last` (today, on a calendar). */
function useRoving(n: number, step: { x: number; y: number } = { x: 1, y: 0 }, last = n - 1) {
  const [at, setAt] = useState<number | null>(null);
  const onKey = (e: React.KeyboardEvent) => {
    const d = { ArrowRight: step.x, ArrowLeft: -step.x, ArrowDown: step.y, ArrowUp: -step.y, Home: -n, End: n }[e.key];
    if (!d) return;
    e.preventDefault();
    setAt((i) => (i == null ? last : Math.max(0, Math.min(last, i + d))));
  };
  return { at, setAt, onKey, onBlur: () => setAt(null) };
}

export type Col = { key: string; label: string; values: number[]; tip: React.ReactNode };

/** Columns over time (a day each), up to two stacked series from the baseline; `series` names them for the legend
 *  (none for one series: the section title says what is drawn). `marks` puts a vermilion dot under a column (a day
 *  something failed); `whole` keeps the ticks to whole numbers (counts). The tooltip is drawn for the eye and said
 *  again, beside the drawing, for a screen reader (what is inside role=img is not read). */
export function Columns({ data, series, label, height = 120, unit, marks, whole }: {
  data: Col[]; series: { name: string; cls: string }[]; label: string; height?: number; unit?: string; marks?: boolean[];
  whole?: boolean;
}) {
  const r = useRoving(data.length);
  const max = Math.max(0, ...data.map((d) => d.values.reduce((a, b) => a + b, 0)));
  const top = niceMax(max, whole);
  // the unit rides on the top tick only
  const fmt = (v: number) => `${Number.isInteger(v) ? big(v) : +v.toFixed(1)}${unit && v === top ? ` ${unit}` : ""}`;
  const shown = r.at != null ? data[r.at] : null;
  const every = data.length > 14 ? 7 : data.length > 7 ? 2 : 1;
  return (
    <figure className="cols" aria-label={label}>
      {series.length > 1 && (
        <figcaption className="legend">{series.map((s) => <span key={s.name} className={s.cls}>{s.name}</span>)}</figcaption>
      )}
      <div className="cols-plot" style={{ height }}>
        <div className="cols-hit" tabIndex={0} role="img" aria-label={`${label}（方向键逐日查看）`}
          onKeyDown={r.onKey} onBlur={r.onBlur} onMouseLeave={() => r.setAt(null)}>
          {[top, top / 2].map((v) => (
            <span key={v} className="cols-grid" style={{ bottom: `${(v / top) * 100}%` }}><em>{fmt(v)}</em></span>
          ))}
          <div className="cols-bars">
            {data.map((d, i) => {
              const total = d.values.reduce((a, b) => a + b, 0);
              return (
                <div key={d.key} className={`col${r.at === i ? " on" : ""}`} onMouseEnter={() => r.setAt(i)}>
                  <div className="col-stack" style={{ height: `${(total / top) * 100}%` }}>
                    {d.values.map((v, k) => v > 0 && (
                      <i key={k} className={series[k]?.cls} style={{ flexGrow: v }} />
                    ))}
                  </div>
                  {marks?.[i] && <b className="col-mark" aria-hidden />}
                </div>
              );
            })}
          </div>
        </div>
        {shown && r.at != null && (
          <div className={`tip${r.at > data.length * 0.6 ? " left" : ""}`} aria-hidden
            style={{ left: `${((r.at + 0.5) / data.length) * 100}%` }}>{shown.tip}</div>
        )}
        <span className="sr" role="status">{shown?.tip}</span>
      </div>
      <div className="cols-x" aria-hidden>
        {data.map((d, i) => <span key={d.key}>{(data.length - 1 - i) % every === 0 ? d.label : ""}</span>)}
      </div>
    </figure>
  );
}

/** The top tick: just above the tallest column, on a step that halves cleanly (the middle tick); for counts, an even
 *  whole number up to 10. */
function niceMax(v: number, whole = false) {
  if (whole && v <= 10) return Math.max(2, Math.ceil(v) + (Math.ceil(v) % 2));
  if (v <= 1) return v <= 0.5 ? 0.5 : 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return ([1, 2, 3, 4, 5, 6, 8, 10].find((s) => v / p <= s) ?? 10) * p;
}

/** Issues made per day as a calendar, weeks across (Monday on top), ink deeper with more. One tab stop: ←/→ a week,
 *  ↑/↓ a day. */
export function Heat({ counts, weeks, label, tip }: {
  counts: Record<string, number>; weeks: number; label: string; tip: (day: Date, n: number) => React.ReactNode;
}) {
  const id = useId();
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const monday = new Date(today);
  monday.setDate(today.getDate() - ((today.getDay() + 6) % 7));
  const start = new Date(monday);
  start.setDate(monday.getDate() - (weeks - 1) * 7);
  const cells: { d: Date; key: string; n: number; future: boolean }[] = [];
  for (let i = 0; i < weeks * 7; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    const key = ymd(d);
    cells.push({ d, key, n: counts[key] || 0, future: d > today });
  }
  const r = useRoving(cells.length, { x: 7, y: 1 }, cells.findIndex((c) => c.key === ymd(today)));
  const months: { col: number; text: string }[] = [];
  for (let w = 0; w < weeks; w++) {
    const d = cells[w * 7].d;
    const m = d.getMonth();
    if (w === 0 ? d.getDate() <= 7 : m !== cells[(w - 1) * 7].d.getMonth()) months.push({ col: w, text: `${m + 1}月` });
  }
  const level = (n: number) => (n <= 0 ? 0 : n === 1 ? 1 : n === 2 ? 2 : n <= 4 ? 3 : 4);
  const at = r.at != null ? cells[r.at] : null;
  const total = cells.reduce((a, c) => a + c.n, 0);
  return (
    <figure className="heat" aria-labelledby={id} style={{ "--weeks": weeks } as React.CSSProperties}>
      <div className="heat-m" aria-hidden>{months.map((m) => <span key={m.col} style={{ gridColumn: m.col + 2 }}>{m.text}</span>)}</div>
      <div className="heat-grid" tabIndex={0} role="img" aria-label={`${label}：近 ${weeks} 周共 ${total} 期（方向键逐日查看）`}
        onKeyDown={r.onKey} onBlur={r.onBlur} onMouseLeave={() => r.setAt(null)}>
        <span className="heat-d" aria-hidden style={{ gridRow: 1 }}>一</span>
        <span className="heat-d" aria-hidden style={{ gridRow: 3 }}>三</span>
        <span className="heat-d" aria-hidden style={{ gridRow: 5 }}>五</span>
        {cells.map((c, i) => (
          <i key={c.key} className={`l${level(c.n)}${c.future ? " f" : ""}${r.at === i ? " on" : ""}`}
            style={{ gridColumn: Math.floor(i / 7) + 2, gridRow: (i % 7) + 1 }}
            onMouseEnter={() => !c.future && r.setAt(i)} onClick={() => !c.future && r.setAt(i)} />
        ))}
        {at && !at.future && (
          <div className={`tip${r.at! / 7 > weeks * 0.6 ? " left" : ""}`} aria-hidden
            style={{ left: `calc(var(--heat-label) + ${(Math.floor(r.at! / 7) + 0.5) / weeks} * (100% - var(--heat-label)))`, top: `${((r.at! % 7) / 7) * 100}%` }}>
            {tip(at.d, at.n)}
          </div>
        )}
      </div>
      <span className="sr" role="status">{at && !at.future ? tip(at.d, at.n) : null}</span>
      <figcaption className="heat-k" id={id}>
        <span>少</span>{[0, 1, 2, 3, 4].map((l) => <i key={l} className={`l${l}`} />)}<span>多</span>
      </figcaption>
    </figure>
  );
}

export const ymd = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

/** Ranked horizontal bars: a label, a bar against the largest, the value written at its end. */
export function HBars({ rows, label }: { rows: { key: string; label: string; value: number; text: string; rest?: boolean }[]; label: string }) {
  const max = Math.max(1e-9, ...rows.map((x) => x.value));
  return (
    <ol className="hbars" aria-label={label}>
      {rows.map((x) => (
        <li key={x.key} className={x.rest ? "rest" : undefined}>
          <span className="hb-l">{x.label}</span>
          <span className="hb-t"><i style={{ width: `${Math.max(1, (x.value / max) * 100)}%` }} /></span>
          <span className="hb-v">{x.text}</span>
        </li>
      ))}
    </ol>
  );
}

const when = (t: number) => {
  const d = new Date(t);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

/** A line over time (0–1), with the last point marked: the week's quota as it was used up and reset; its first and
 *  last days under it. Hover or the arrow keys pick a reading. The dots are HTML over the stretched drawing, so they
 *  stay round. */
export function Spark({ points, label, height = 36 }: { points: [number, number][]; label: string; height?: number }) {
  const r = useRoving(points.length);
  if (points.length < 2) return null;
  const t0 = points[0][0], t1 = points[points.length - 1][0];
  const x = (t: number) => ((t - t0) / Math.max(1, t1 - t0)) * 100;
  const y = (v: number) => (1 - v) * (height - 6) + 3;
  const d = points.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(2)},${y(v).toFixed(2)}`).join("");
  const last = points[points.length - 1];
  const at = r.at != null ? points[r.at] : null;
  const pick = (e: React.MouseEvent<HTMLDivElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const t = t0 + ((e.clientX - box.left) / box.width) * (t1 - t0);
    let best = 0;
    points.forEach((p, i) => { if (Math.abs(p[0] - t) < Math.abs(points[best][0] - t)) best = i; });
    r.setAt(best);
  };
  const md = (t: number) => `${new Date(t).getMonth() + 1}/${new Date(t).getDate()}`;
  return (
    <figure className="spark">
      <div className="sp-plot" style={{ height }} tabIndex={0} role="img" aria-label={`${label}（方向键逐次查看）`}
        onKeyDown={r.onKey} onBlur={r.onBlur} onMouseMove={pick} onMouseLeave={() => r.setAt(null)}>
        <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" aria-hidden>
          <path className="sp-a" d={`${d}L100,${height}L0,${height}Z`} />
          <path className="sp-l" d={d} vectorEffect="non-scaling-stroke" />
        </svg>
        <i className="sp-e" style={{ left: `${x(last[0])}%`, top: y(last[1]) }} />
        {at && <i className="sp-e on" style={{ left: `${x(at[0])}%`, top: y(at[1]) }} />}
        {at && (
          <div className={`tip${x(at[0]) > 60 ? " left" : ""}`} aria-hidden style={{ left: `${x(at[0])}%` }}>
            <b>剩 {pct(at[1])}</b> {when(at[0])}
          </div>
        )}
      </div>
      <span className="sr" role="status">{at ? `剩 ${pct(at[1])}，${when(at[0])}` : ""}</span>
      <figcaption className="sp-x" aria-hidden><span>{md(t0)}</span><span>{md(t1)}</span></figcaption>
    </figure>
  );
}
