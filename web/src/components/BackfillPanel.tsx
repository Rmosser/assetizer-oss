import { useEffect, useRef, useState } from "react";
import { at8, BACKFILL_BAD, BACKFILL_STOP, hourMinute8, hoursText, nextHour } from "../util";
import { Switch } from "./channel/bits";
import { hrs } from "./Figures";
import { type Save, Stepper } from "./Knobs";

const pct = (v: number) => `${Math.round(v * 100)}%`;
/** 到 15:00, 到明天 07:00 */
const till = (iso: string) => { const at = at8(iso); return /^\d+:/.test(at) ? `到 ${at}` : `到${at}`; };

/** What 补录 does now, in a few words (ingest.backfill_view). */
export function paceText(v: any): string {
  if (v.active > v.slots) return `收尾 ${v.active} 期 · 之后 ${v.slots} 路`;
  if (v.fast) return v.until ? `${v.slots} 路 · ${till(v.until)}` : `${v.slots} 路`;
  return v.next ? `1 路 · ${at8(v.next)} 起 ${v.lanes} 路` : "1 路";
}

/** The 补录 line in the 系统 list: how much, how fast, or what stands in its way. */
export function backfillLine(v: any): { lamp: "ok" | "bad" | "idle" | "warn"; value: string } {
  if (!v.pending && !v.active) return { lamp: "idle", value: "没有排队" };
  if (!v.pending) return { lamp: "ok", value: `收尾 ${v.active} 期` };
  const n = `${v.pending} 期`;
  if (v.stop) return { lamp: BACKFILL_BAD.includes(v.stop) ? "bad" : v.stop === "queued" || v.stop === "trial" ? "ok" : "warn", value: `${n} · ${BACKFILL_STOP[v.stop] || v.stop}` };
  return { lamp: "ok", value: v.active > v.slots ? `${n} · 收尾中` : `${n} · ${v.slots} 路` };
}

/** A day of 24 hours to paint: press on an hour and drag across the others, off hours turn on and on hours off. */
function HourStrip({ hours, onChange }: { hours: number[]; onChange: (next: number[]) => void }) {
  const stroke = useRef<{ on: boolean; last: number; id: number } | null>(null);
  const on = new Set(hours);
  const [now, minute] = hourMinute8();
  const paint = (from: number, to: number, value: boolean) => {
    const next = new Set(stroke.current ? latest.current : hours);
    for (let h = Math.min(from, to); h <= Math.max(from, to); h++) { if (value) next.add(h); else next.delete(h); }
    latest.current = [...next].sort((a, b) => a - b);
    onChange(latest.current);
  };
  const latest = useRef(hours);
  const hourAt = (x: number, y: number) => {
    const h = document.elementFromPoint(x, y)?.closest<HTMLElement>("[data-h]")?.dataset.h;
    return h == null ? null : Number(h);
  };
  return (
    <div className="bf-strip" role="group" aria-label="每天高频的钟点"
      onPointerDown={(e) => {
        const h = hourAt(e.clientX, e.clientY);
        if (h == null || e.button !== 0) return;
        e.currentTarget.setPointerCapture(e.pointerId);
        latest.current = hours;
        const value = !on.has(h);
        paint(h, h, value);
        stroke.current = { on: value, last: h, id: e.pointerId };
      }}
      onPointerMove={(e) => {
        const s = stroke.current;
        if (!s || s.id !== e.pointerId) return;
        const h = hourAt(e.clientX, e.clientY);
        if (h == null || h === s.last) return;
        paint(s.last, h, s.on);
        s.last = h;
      }}
      onPointerUp={() => { stroke.current = null; }}
      onPointerCancel={() => { stroke.current = null; }}>
      {Array.from({ length: 24 }, (_, h) => (
        <button key={h} type="button" data-h={h} className={`bf-hr${on.has(h) ? " on" : ""}`} aria-pressed={on.has(h)} aria-label={`${String(h).padStart(2, "0")}:00`}
          title={`${String(h).padStart(2, "0")}:00–${String((h + 1) % 24).padStart(2, "0")}:00`}
          onClick={(e) => { if (e.detail === 0) { latest.current = hours; paint(h, h, !on.has(h)); } }}>
          {h % 3 === 0 && <small className={h % 6 ? "third" : ""}>{h}</small>}
          {h === now && <i className="now" style={{ left: `${(minute / 60) * 100}%` }} aria-hidden />}
        </button>
      ))}
    </div>
  );
}

/** 现在高频's end: not at all, or the nth whole hour from now. Shown at once, saved a moment after the last click. */
function BurstEnd({ until, save }: { until: string | null; save: Save }) {
  const toN = (u: string | null) => (u ? Math.max(1, Math.round((+new Date(u) - +new Date(nextHour(1))) / 3600e3) + 1) : 0);
  const [edit, setEdit] = useState<number | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const n = edit ?? toN(until);
  const go = (d: number) => {
    const v = Math.min(48, Math.max(0, n + d));
    setEdit(v);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(async () => {
      await save({ backfill_burst: { until: v ? nextHour(v) : null } });
      window.setTimeout(() => setEdit(null), 800);
    }, 600);
  };
  return (
    <span className="kn-step bf-end" role="group" aria-label="现在高频到什么时候">
      <button aria-label="提早" disabled={n <= 0} onClick={() => go(-1)}>−</button>
      <b className="num">{n ? till(nextHour(n)) : "不限时"}</b>
      <button aria-label="延后" disabled={n >= 48} onClick={() => go(1)}>+</button>
    </span>
  );
}

/** 补录: how much waits and at what pace, then what sets the pace; what it leaves to new videos is at the end. */
export function BackfillPanel({ kn, view, save }: { kn: any; view: any; save: Save }) {
  const [hours, setHours] = useState<number[] | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const shown: number[] = hours ?? kn.backfill_hours;
  const paint = (next: number[]) => {
    setHours(next);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(async () => {
      await save({ backfill_hours: next });
      window.setTimeout(() => setHours(null), 800);
    }, 500);
  };
  useEffect(() => () => clearTimeout(timer.current), []);
  // more lanes than the box prints at once would be a setting that does nothing: it takes 同时印 up with it
  const saveLanes: Save = (b) => {
    const v = b.backfill_fast_slots as number;
    return save({ ...b, ...(v > kn.worker_slots ? { worker_slots: v } : {}) });
  };
  const stop = view.pending ? view.stop : null;
  return (
    <div className="bf">
      <div className="bf-head">
        <div className="bf-fig">
          <b className="num">{view.pending}</b><small>期</small>
          {view.seconds > 0 && <span>视频 {hrs(view.seconds)} 小时</span>}
        </div>
        <div className="bf-pace">
          <b>{paceText(view)}</b>
          {stop && <span className={BACKFILL_BAD.includes(stop) ? "bad" : stop === "queued" || stop === "trial" ? "" : "warn"}>{BACKFILL_STOP[stop] || stop}</span>}
        </div>
      </div>

      <div className="kn-line">
        <span>现在高频</span>
        {kn.backfill_burst && <BurstEnd until={kn.backfill_burst.until} save={save} />}
        <Switch on={!!kn.backfill_burst} label="现在高频" onChange={(on) => save({ backfill_burst: on ? { until: null } : null })} />
      </div>

      <div className="kn-line">
        <span>每天高频</span>
        <b className="bf-read num">{hoursText(shown) || "—"}</b>
      </div>
      <HourStrip hours={shown} onChange={paint} />

      <div className="kn-line">
        <span>高频路数</span>
        <Stepper k="backfill_fast_slots" value={kn.backfill_fast_slots} lo={1} hi={16} save={saveLanes} label="高频路数" />
      </div>

      <h4 className="bf-sub">留给新视频</h4>
      <div className="kn-line"><span>每周额度</span>
        <Stepper k="backfill_min_weekly" value={kn.backfill_min_weekly} lo={0} hi={0.9} step={0.1} fmt={pct} save={save} label="补录留给新视频的周额度" /></div>
      <div className="kn-line"><span>5 小时额度</span>
        <Stepper k="backfill_min_5h" value={kn.backfill_min_5h} lo={0} hi={0.9} step={0.1} fmt={pct} save={save} label="补录留给新视频的 5 小时额度" /></div>
    </div>
  );
}
