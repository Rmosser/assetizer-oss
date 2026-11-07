import { useRef, useState } from "react";
import { backfillStopLamp, paceText, till } from "../box";
import { BACKFILL_STOP, hourMinute8, hoursText, nextHour } from "../util";
import { Switch } from "./channel/bits";
import { hrs } from "./Figures";
import { Row, type Save, Stepper, useLater } from "./Knobs";

const pct = (v: number) => `${Math.round(v * 100)}%`;

/** A day of 24 hours to paint: press on an hour and drag across the others, off hours turn on and on hours off. A finger
 *  scrolls the page as anywhere else: it paints on a tap, or once it moves sideways along the strip. The keyboard enters on
 *  one hour and goes along with the arrows. */
function HourStrip({ hours, onChange }: { hours: number[]; onChange: (next: number[]) => void }) {
  const stroke = useRef<{ on: boolean; last: number; id: number; x: number; held: boolean } | null>(null);
  const on = new Set(hours);
  const [now, minute] = hourMinute8();
  const latest = useRef(hours);
  const [stop, setStop] = useState(now);
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const d = ({ ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 } as Record<string, number>)[e.key];
    const to = e.key === "Home" ? 0 : e.key === "End" ? 23 : d ? (stop + d + 24) % 24 : null;
    if (to == null) return;
    e.preventDefault();
    setStop(to);
    e.currentTarget.querySelector<HTMLElement>(`[data-h="${to}"]`)?.focus();
  };
  const paint = (from: number, to: number, value: boolean) => {
    const next = new Set(latest.current);
    for (let h = Math.min(from, to); h <= Math.max(from, to); h++) { if (value) next.add(h); else next.delete(h); }
    latest.current = [...next].sort((a, b) => a - b);
    onChange(latest.current);
  };
  const hourAt = (x: number, y: number) => {
    const h = document.elementFromPoint(x, y)?.closest<HTMLElement>("[data-h]")?.dataset.h;
    return h == null ? null : Number(h);
  };
  return (
    <div className="bf-strip" role="group" aria-label="每天高频的钟点" onKeyDown={onKey}
      onPointerDown={(e) => {
        const h = hourAt(e.clientX, e.clientY);
        if (h == null || e.button !== 0) return;
        latest.current = hours;
        const value = !on.has(h);
        const held = e.pointerType === "touch";  // may yet turn out to be a scroll
        if (!held) { e.currentTarget.setPointerCapture(e.pointerId); paint(h, h, value); }
        stroke.current = { on: value, last: h, id: e.pointerId, x: e.clientX, held };
      }}
      onPointerMove={(e) => {
        const s = stroke.current;
        if (!s || s.id !== e.pointerId) return;
        if (s.held) {
          if (Math.abs(e.clientX - s.x) < 6) return;
          s.held = false;
          e.currentTarget.setPointerCapture(e.pointerId);
          paint(s.last, s.last, s.on);
        }
        const h = hourAt(e.clientX, e.clientY);
        if (h == null || h === s.last) return;
        paint(s.last, h, s.on);
        s.last = h;
      }}
      onPointerUp={() => {
        const s = stroke.current;
        stroke.current = null;
        if (s?.held) paint(s.last, s.last, s.on);  // a tap
      }}
      onPointerCancel={() => { stroke.current = null; }}>
      {Array.from({ length: 24 }, (_, h) => (
        <button key={h} type="button" data-h={h} tabIndex={h === stop ? 0 : -1} className={`bf-hr${on.has(h) ? " on" : ""}`} aria-pressed={on.has(h)} aria-label={`${String(h).padStart(2, "0")}:00`}
          title={`${String(h).padStart(2, "0")}:00–${String((h + 1) % 24).padStart(2, "0")}:00`} onFocus={() => setStop(h)}
          onClick={(e) => { if (e.detail === 0) { latest.current = hours; paint(h, h, !on.has(h)); } }}>
          {h % 3 === 0 && <small className={h % 6 ? "third" : ""}>{h}</small>}
          {h === now && <i className="now" style={{ left: `${(minute / 60) * 100}%` }} aria-hidden />}
        </button>
      ))}
    </div>
  );
}

/** 现在高频's end: not at all, or the nth whole hour from now; `until` and `set` are BackfillPanel's (useLater). */
function BurstEnd({ until, set }: { until: string | null; set: (until: string | null) => void }) {
  const n = until ? Math.max(1, Math.round((+new Date(until) - +new Date(nextHour(1))) / 3600e3) + 1) : 0;
  const go = (d: number) => {
    const v = Math.min(48, Math.max(0, n + d));
    if (v !== n) set(v ? nextHour(v) : null);
  };
  return (
    <span className="kn-step bf-end" role="group" aria-label="现在高频到什么时候">
      <button type="button" aria-label="提早" aria-disabled={n <= 0} onClick={() => go(-1)}>−</button>
      <b className="num">{until ? till(until) : "不限时"}</b>
      <button type="button" aria-label="延后" aria-disabled={n >= 48} onClick={() => go(1)}>+</button>
    </span>
  );
}

/** 补录: how much waits and at what pace, then what sets the pace; what it leaves to new videos is at the end. */
export function BackfillPanel({ kn, view, save }: { kn: any; view: any; save: Save }) {
  const [shown, paint] = useLater<number[]>(kn.backfill_hours, (v) => save({ backfill_hours: v }), 500);
  // switched off, an end still waiting to be saved would switch it on again
  const [until, setUntil, dropUntil] = useLater<string | null>(kn.backfill_burst?.until ?? null, (v) => save({ backfill_burst: { until: v } }), 600);
  // more lanes than the box prints at once would be a setting that does nothing: it takes 同时印 up with it
  const saveLanes: Save = (b) => {
    const v = b.backfill_fast_slots as number;
    return save({ ...b, ...(v > kn.worker_slots ? { worker_slots: v } : {}) });
  };
  const stop = view.pending ? view.stop : null;
  const lamp = stop && backfillStopLamp(stop);
  return (
    <>
      <Row name="排队">{view.pending ? `${view.pending} 期${view.seconds > 0 ? ` · 视频 ${hrs(view.seconds)} 小时` : ""}` : "没有"}</Row>
      {(view.pending > 0 || view.active > 0) && (
        <Row name="现在" note={stop && <span className={lamp === "ok" ? undefined : lamp === "bad" ? "zhu" : "warn"}>{BACKFILL_STOP[stop] || stop}</span>}>{paceText(view)}</Row>
      )}
      <Row name="现在高频">
        {kn.backfill_burst && <BurstEnd until={until} set={setUntil} />}
        <Switch on={!!kn.backfill_burst} label="现在高频" onChange={(on) => {
          if (!on) dropUntil(null);
          save({ backfill_burst: on ? { until: null } : null });
        }} />
      </Row>
      <div className="st-row st-wide">
        <div className="st-k">每天高频</div>
        <div className="st-v"><span className="num">{hoursText(shown) || "没有"}</span></div>
        <HourStrip hours={shown} onChange={paint} />
      </div>
      <Row name="高频路数"><Stepper k="backfill_fast_slots" value={kn.backfill_fast_slots} lo={1} hi={16} fmt={(v) => `${v} 路`} save={saveLanes} label="高频路数" /></Row>
      <h3 className="st-group">留给新视频</h3>
      <Row name="每周额度"><Stepper k="backfill_min_weekly" value={kn.backfill_min_weekly} lo={0} hi={0.9} step={0.1} fmt={pct} save={save} label="补录留给新视频的周额度" /></Row>
      <Row name="5 小时额度"><Stepper k="backfill_min_5h" value={kn.backfill_min_5h} lo={0} hi={0.9} step={0.1} fmt={pct} save={save} label="补录留给新视频的 5 小时额度" /></Row>
    </>
  );
}
