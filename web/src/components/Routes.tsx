import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useMedia } from "../hooks";
import { pct } from "./Figures";
import type { Save } from "./Knobs";
import { Sheet, type SheetHandle } from "./Sheet";

const PROVIDER: Record<string, string> = { agy: "Gemini", tuzi: "Gemini（兔子）" };
/** the stages that watch video: anything but Gemini gets frames and a transcript in its place */
const VIDEO_STEPS = new Set(["understanding", "boundary"]);
const BLIND = "看不了视频";
const VENDOR_FAMILY: Record<string, string> = { codex: "chatgpt", claude: "claude", grok: "grok", gemini: "gemini", agy: "gemini" };
/** one name per subscription, whichever part of the page says it */
export const VENDOR_NAME: Record<string, string> = { agy: "Gemini", codex: "ChatGPT", claude: "Claude", grok: "Grok" };
const VENDOR_ORDER = ["agy", "codex", "claude", "grok"];
const isAgy = (route: string) => route === "agy" || route.startsWith("agy/");
/** Off the list of whoever lists it: agy for its models, magpie for the rest. No list yet says nothing. */
const withdrawn = (route: string, models: any[]) => route !== "tuzi" && route !== "agy" && !models.some((m) => m.id === route)
  && models.some((m) => (m.vendor === "agy") === isAgy(route));

/** A route's name: the provider's, the model's as magpie lists it, else its id with the provider left off */
function modelName(route: string, models: any[]): string {
  return PROVIDER[route] || models.find((m) => m.id === route)?.name || route.split("/").pop() || route;
}

/** Whose color a route wears on this page */
export function familyOf(route: string, models: any[]): string {
  if (route === "agy" || route === "tuzi") return "gemini";
  const vendor = models.find((m) => m.id === route)?.vendor ?? (route.includes("/") && !route.startsWith("group/") ? route.split("/")[0] : "");
  return VENDOR_FAMILY[vendor] || "other";
}

/** What a subscription has left, beside its models where one is being picked */
export type Left = { left?: number; note?: string; bad?: boolean };

type Opt = { id: string; name: string; off?: boolean; tag?: string; zhu?: boolean; family?: string };
type Section = { key: string; name?: string; tag?: string; family?: string; left?: Left | null; opts: Opt[] };

function sections(value: string, models: any[], video: boolean, lefts: Record<string, Left | null>): Section[] {
  const opt = (m: any, told = false): Opt => ({ id: m.id, name: m.name, off: video && !m.watches, tag: video && !m.watches && !told ? BLIND : undefined });
  // on a stage that watches video everyone but Gemini gets frames: said once per subscription, not on every row
  const framed = (ms: any[]) => (!video ? undefined : ms.some((m) => m.watches) ? "抽帧" : BLIND);
  const known = value === "agy" || value === "tuzi" || models.some((m) => m.id === value);
  const out = withdrawn(value, models);
  const own = models.filter((m) => !m.group);
  const vendors = [...new Set<string>(own.map((m) => m.vendor))].sort((a, b) => {
    const at = (v: string) => (VENDOR_ORDER.includes(v) ? VENDOR_ORDER.indexOf(v) : VENDOR_ORDER.length);
    return at(a) - at(b);
  });
  const groups = models.filter((m) => m.group);
  return [
    ...(known ? [] : [{ key: "gone", opts: [{ id: value, name: modelName(value, models), tag: out ? "已取消" : undefined, zhu: out, family: familyOf(value, models) }] }]),
    ...vendors.map((v) => {
      const ms = own.filter((m) => m.vendor === v);
      const tag = v === "agy" ? undefined : framed(ms);  // Gemini watches the video itself
      const opts: Opt[] = ms.map((m) => opt(m, tag === BLIND));
      return { key: v, name: VENDOR_NAME[v] || (ms[0].label || "").split(" · ").pop() || v, tag, family: VENDOR_FAMILY[v] || "other", left: lefts[v],
        opts: v === "agy" && value === "tuzi" ? [...opts, { id: "tuzi", name: PROVIDER.tuzi }] : opts };
    }),
    ...(groups.length ? [{ key: "group", name: "路由分组", tag: framed(groups), opts: groups.map((m) => ({ ...opt(m, framed(groups) === BLIND), family: familyOf(m.id, models) })) }] : []),
  ];
}

function LeftMark({ l }: { l?: Left | null }) {
  if (!l) return null;
  if (l.note) return <span className={`eg-left${l.bad ? " bad" : ""}`}>{l.note}</span>;
  if (l.left == null) return null;
  return (
    <span className={`eg-left${l.left < 0.2 ? " low" : ""}`} aria-label={`剩 ${pct(l.left)}`}>
      <i aria-hidden><b style={{ width: `${Math.max(l.left * 100, 3)}%` }} /></i>{pct(l.left)}
    </span>
  );
}

const tick = (
  <svg className="tick" viewBox="0 0 14 14" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d="M2.8 7.4 5.7 10.2 11.2 4" />
  </svg>
);

function Option({ o, on, solo, pick }: { o: Opt; on: boolean; solo?: { family?: string; left?: Left | null }; pick: (id: string) => void }) {
  const family = solo?.family || o.family;
  return (
    <button type="button" role="option" aria-selected={on} aria-disabled={o.off || undefined} tabIndex={-1}
      className={`eg-opt${solo || o.family ? " marked" : ""}${solo ? " solo" : ""}${o.zhu ? " zhu" : ""}${family ? ` p-${family}` : ""}`}
      onClick={() => { if (!o.off) pick(o.id); }}>
      {family && <i className="pig" aria-hidden />}
      <span className="n">{o.name}</span>
      {o.tag && <em>{o.tag}</em>}
      {solo && <LeftMark l={solo.left} />}
      {on && tick}
    </button>
  );
}

/** A stage and the model it goes to. The list opens under the cell, with what each subscription has left beside its
 *  models; on a phone it comes up as a sheet. */
function RoutePicker({ value, models, video, name, live, bad, lefts, onPick }: {
  value: string; models: any[]; video: boolean; name: string; live: boolean; bad: boolean;
  lefts: Record<string, Left | null>; onPick: (r: string) => void;
}) {
  const phone = useMedia("(max-width: 720px)");
  const [open, setOpen] = useState(false);
  const [place, setPlace] = useState<{ up: boolean; end: boolean; h: number } | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const sheet = useRef<SheetHandle>(null);
  const typed = useRef({ s: "", at: 0, space: false });
  const id = useId();
  const gemini = isAgy(value) || value === "tuzi";
  const gone = withdrawn(value, models);

  const close = (refocus = true) => {
    setOpen(false);
    setPlace(null);
    if (refocus) trigger.current?.focus();
  };
  const pick = (r: string) => {
    if (r !== value) onPick(r);
    if (phone) sheet.current?.close();
    else close();
  };
  useEffect(() => {
    if (!open || phone) return;
    const away = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) close(false); };
    const gone = () => close(false);
    document.addEventListener("pointerdown", away);
    addEventListener("resize", gone);
    return () => { document.removeEventListener("pointerdown", away); removeEventListener("resize", gone); };
  }, [open, phone]); // eslint-disable-line react-hooks/exhaustive-deps
  // down if the list fits, else up if it fits there, else whichever side has more room, scrolling inside
  useLayoutEffect(() => {
    if (!open || phone || !pop.current || !root.current) return;
    const r = root.current.getBoundingClientRect();
    const want = pop.current.scrollHeight + 2;
    const below = innerHeight - r.bottom - 22, above = r.top - 86;
    const up = want > below && (want <= above || above > below);
    setPlace({ up, end: r.left + Math.max(r.width, 304) > document.documentElement.clientWidth - 16, h: Math.min(want, up ? above : below) });
  }, [open, phone]);
  // once it shows, the keyboard goes on from the model in use
  useEffect(() => {
    if (!place || !pop.current) return;
    const on = pop.current.querySelector<HTMLElement>('[aria-selected="true"]:not([aria-disabled="true"])') || pop.current.querySelector<HTMLElement>('[role="option"]:not([aria-disabled="true"])');
    on?.focus({ preventScroll: true });
    on?.scrollIntoView({ block: "nearest" });
  }, [place]);

  const onKey = (e: React.KeyboardEvent<HTMLElement>) => {
    const opts = [...e.currentTarget.querySelectorAll<HTMLElement>('[role="option"]:not([aria-disabled="true"])')];
    const at = opts.indexOf(document.activeElement as HTMLElement);
    const go = (i: number) => { const o = opts[(i + opts.length) % opts.length]; o?.focus({ preventScroll: true }); o?.scrollIntoView({ block: "nearest" }); };
    if (e.key === "ArrowDown") { e.preventDefault(); go(at + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); go(at < 0 ? -1 : at - 1); }
    else if (e.key === "Home") { e.preventDefault(); go(0); }
    else if (e.key === "End") { e.preventDefault(); go(opts.length - 1); }
    else if (e.key === "Escape" && !phone) { e.preventDefault(); close(); }
    else if (e.key === "Tab" && !phone) close(false);
    else if (e.key.length === 1 && !e.metaKey && !e.ctrlKey) {
      const now = Date.now();
      const going = now - typed.current.at < 700 ? typed.current.s : "";
      if (e.key === " " && !going) return; // a space on its own picks the row in focus; in the middle of a name it is part of the name
      e.preventDefault();
      typed.current = { s: going + e.key.toLowerCase(), at: now, space: e.key === " " };
      const n = opts.findIndex((o) => (o.querySelector(".n")?.textContent || "").toLowerCase().startsWith(typed.current.s));
      if (n >= 0) go(n);
    }
  };

  const list = (
    <div role="listbox" aria-label={name} id={id} className="eg-pick-list" onKeyDown={onKey}
      onKeyUp={(e) => { if (e.key === " " && typed.current.space) { e.preventDefault(); typed.current.space = false; } }}>
      {sections(value, models, video, lefts).map((s) => (
        <div key={s.key} role="group" aria-label={s.name} className={s.family ? `p-${s.family}` : undefined}>
          {s.opts.length === 1 && s.name && s.key !== "group" ? (
            <Option o={{ ...s.opts[0], tag: s.opts[0].tag || s.tag }} on={s.opts[0].id === value} solo={{ family: s.family, left: s.left }} pick={pick} />
          ) : (
            <>
              {s.name && <div className="eg-sect">{s.family && <i className="pig" aria-hidden />}<span>{s.name}</span>{s.tag && <em>{s.tag}</em>}<LeftMark l={s.left} /></div>}
              {s.opts.map((o) => <Option key={o.id} o={o} on={o.id === value} pick={pick} />)}
            </>
          )}
        </div>
      ))}
    </div>
  );

  return (
    <div ref={root} className={`eg-route p-${familyOf(value, models)}${bad || gone ? " bad" : ""}${live ? " live" : ""}${open ? " open" : ""}`}>
      <button ref={trigger} type="button" className="eg-route-b" aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id : undefined}
        aria-label={`${name}：${modelName(value, models)}`} onClick={() => (open ? close() : setOpen(true))}
        onKeyDown={(e) => { if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); setOpen(true); } }}>
        <span className="eg-route-k">{name}</span>
        <span className="eg-route-v">
          <span className="t">{modelName(value, models)}</span>
          {gone ? <em>已取消</em> : video && !gemini ? <em>抽帧</em> : null}
          <span className="chev" aria-hidden />
        </span>
      </button>
      {open && !phone && (
        <div ref={pop} className={`eg-pick${place?.up ? " up" : ""}${place?.end ? " end" : ""}${place ? " placed" : ""}`} style={place ? { maxHeight: place.h } : undefined}>
          {list}
        </div>
      )}
      {open && phone && <Sheet ref={sheet} title={name} onClose={() => close(false)}><div className="eg-pick-sheet">{list}</div></Sheet>}
    </div>
  );
}

/** 分工: which model each stage uses (stage_providers over llm_provider), from what magpie lists */
export function RouteCells({ kn, steps, label, save, models, routes, missing, live, lefts }: {
  kn: any; steps: string[]; label: Record<string, string>; save: Save; models: any[];
  routes: { default: string; stages: Record<string, string> }; missing: string[]; live: string[]; lefts: Record<string, Left | null>;
}) {
  // a pick shows at once; the page's next read of the box replaces it
  const [picks, setPicks] = useState<Record<string, string>>({});
  useEffect(() => setPicks({}), [kn]);
  const sending = useRef<Promise<void>>(Promise.resolve());
  const main = picks.default || routes.default;
  const route = (k: string) => picks[k] || routes.stages[k] || main;
  const pick = (k: string, r: string) => {
    // 其余 going to a model that cannot watch: the stages that watch and had no route of their own stay where they were going
    const stay = k === "default" && models.find((m) => m.id === r)?.watches === false
      ? Object.fromEntries(steps.filter((s) => VIDEO_STEPS.has(s)).map((s) => [s, route(s)])) : {};
    setPicks((was) => ({ ...was, ...stay, [k]: r }));
    sending.current = sending.current.then(() => save(k === "default" ? { llm_provider: r } : { stage_route: { [k]: r } }));
  };
  return (
    <div className="eg-routes">
      {steps.map((k) => (
        <RoutePicker key={k} value={route(k)} models={models} video={VIDEO_STEPS.has(k)} name={label[k]} live={live.includes(k)}
          bad={missing.includes(route(k))} lefts={lefts} onPick={(r) => pick(k, r)} />
      ))}
      <RoutePicker value={main} models={models} video={false} name="其余" live={false} bad={missing.includes(main)} lefts={lefts} onPick={(r) => pick("default", r)} />
    </div>
  );
}
