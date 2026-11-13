import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useMedia } from "../hooks";
import { linkProps } from "../router";
import { VENDOR_NAME } from "../util";
import { pct } from "./Figures";
import type { Save } from "./Knobs";
import { Sheet, type SheetHandle } from "./Sheet";

const PROVIDER: Record<string, string> = { agy: "Gemini", tuzi: "Gemini（兔子）" };
/** the stages that watch video: anything but Gemini gets frames and a transcript in its place */
const VIDEO_STEPS = new Set(["understanding", "boundary", "channel_draft"]);
const BLIND = "看不了视频";
const VENDOR_FAMILY: Record<string, string> = { codex: "chatgpt", claude: "claude", grok: "grok", gemini: "gemini", agy: "gemini" };
const VENDOR_ORDER = ["agy", "codex", "claude", "grok"];
const isAgy = (route: string) => route === "agy" || route.startsWith("agy/");
/** Where a picker may also say no model at all (knobs.NO_FALLBACK) */
export const NO_ROUTE = "off";
const LEVELLED = /^(.*)-(low|medium|high|xhigh)$/;
/** The model a route is, as listed. agy lists each model once, at the box's level: a route kept at another level it
 * lists (gemini-4-hydrogen-xhigh, listed apart before its levels were read) is that model (providers.agy.listed) */
function listed(route: string, models: any[]): any {
  const lv = isAgy(route) ? LEVELLED.exec(route) : null;
  return models.find((m) => m.id === route) ?? (lv ? models.find((m) => LEVELLED.exec(m.id)?.[1] === lv[1] && m.levels?.includes(lv[2])) : undefined);
}
/** Off the list of whoever lists it: agy for its models, magpie for the rest. No list yet says nothing. */
const withdrawn = (route: string, models: any[]) => route !== NO_ROUTE && route !== "tuzi" && route !== "agy" && !listed(route, models)
  && models.some((m) => (m.vendor === "agy") === isAgy(route));

/** A route's name: the provider's, the model's as magpie lists it, else its id with the provider left off */
function modelName(route: string, models: any[]): string {
  if (route === NO_ROUTE) return "不换";
  return PROVIDER[route] || listed(route, models)?.name || route.split("/").pop() || route;
}

/** How hard a model thinks (providers.LEVELS), lowest first, and what the page calls each */
const LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"];
const LEVEL_NAME: Record<string, string> = { minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "超高", max: "极高", ultra: "极限" };
const GEMINI_LEVELS = ["low", "medium", "high"];
/** The levels a route takes: as magpie or agy lists the model; Gemini's three for 兔子 */
export function levelsOf(route: string, models: any[]): string[] {
  if (route === "tuzi") return GEMINI_LEVELS;
  return ((listed(route, models)?.levels || []) as string[]).filter((x) => LEVELS.includes(x));
}
/** The level of `have` a set one runs at (providers.nearest): itself, else the closest, ties going lower */
function nearest(level: string, have: string[]): string | undefined {
  const at = LEVELS.indexOf(level);
  return [...have].sort((a, b) => Math.abs(LEVELS.indexOf(a) - at) - Math.abs(LEVELS.indexOf(b) - at) || LEVELS.indexOf(a) - LEVELS.indexOf(b))[0];
}
/** A cell's level as it shows: none when the code decides, or the model does not think */
const shownLevel = (route: string, models: any[], set?: string | null) => (set ? nearest(set, levelsOf(route, models)) : undefined);

/** What a cell's level of thinking is set to, and how it is changed; null: the code decides, call by call */
export type Effort = { value: string | null; onPick: (level: string | null) => void };

/** Whose color a route wears on this page */
export function familyOf(route: string, models: any[]): string {
  if (route === "agy" || route === "tuzi") return "gemini";
  const vendor = listed(route, models)?.vendor ?? (route.includes("/") && !route.startsWith("group/") ? route.split("/")[0] : "");
  return VENDOR_FAMILY[vendor] || "other";
}

/** What a subscription has left, beside its models where one is being picked */
export type Left = { left?: number; note?: string; bad?: boolean };

type Opt = { id: string; name: string; off?: boolean; tag?: string; zhu?: boolean; family?: string; left?: Left | null };
type Section = { key: string; name?: string; tag?: string; family?: string; left?: Left | null; opts: Opt[] };

function sections(value: string, models: any[], video: boolean, lefts: Record<string, Left | null>, none: boolean): Section[] {
  const opt = (m: any, told = false): Opt => ({ id: m.id, name: m.name, off: video && !m.watches, tag: video && !m.watches && !told ? BLIND : undefined });
  // on a stage that watches video everyone but Gemini gets frames: said once per subscription, not on every row
  const framed = (ms: any[]) => (!video ? undefined : ms.some((m) => m.watches) ? "抽帧" : BLIND);
  // a route kept at another level of a listed model (listed) stays a row of its own, so the list shows what is picked
  const known = value === NO_ROUTE || value === "agy" || value === "tuzi" || models.some((m) => m.id === value);
  const out = withdrawn(value, models);
  const own = models.filter((m) => !m.group);
  const vendors = [...new Set<string>(own.map((m) => m.vendor))].sort((a, b) => {
    const at = (v: string) => (VENDOR_ORDER.includes(v) ? VENDOR_ORDER.indexOf(v) : VENDOR_ORDER.length);
    return at(a) - at(b);
  });
  const groups = models.filter((m) => m.group);
  return [
    ...(none ? [{ key: NO_ROUTE, opts: [{ id: NO_ROUTE, name: modelName(NO_ROUTE, models) }] }] : []),
    ...(known ? [] : [{ key: "gone", opts: [{ id: value, name: modelName(value, models), tag: out ? "已取消" : undefined, zhu: out, family: familyOf(value, models) }] }]),
    ...vendors.map((v) => {
      const ms = own.filter((m) => m.vendor === v);
      const tag = v === "agy" ? undefined : framed(ms);  // Gemini watches the video itself
      // agy keeps a quota per group of models (lefts by model): where its models draw on more than one, each says what its group has left
      const each = v === "agy" && new Set(ms.map((m) => m.quota ?? m.id)).size > 1;
      const opts: Opt[] = ms.map((m) => ({ ...opt(m, tag === BLIND), left: each ? lefts[m.id] : undefined }));
      return { key: v, name: VENDOR_NAME[v] || (ms[0].label || "").split(" · ").pop() || v, tag, family: VENDOR_FAMILY[v] || "other",
        left: v !== "agy" ? lefts[v] : each ? undefined : lefts[ms[0].id],
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
      <LeftMark l={solo ? solo.left : o.left} />
      {on && tick}
    </button>
  );
}

/** The levels the model in use takes, under its list; 自动 leaves it to each call */
function Think({ levels, value, pick, back }: { levels: string[]; value?: string; pick: (l: string | null) => void; back: () => void }) {
  const opts: (string | null)[] = [null, ...levels];
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const bs = [...e.currentTarget.querySelectorAll<HTMLButtonElement>("button")];
    const at = bs.indexOf(document.activeElement as HTMLButtonElement);
    const go = (i: number) => bs[(i + bs.length) % bs.length]?.focus();
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); go(at + 1); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); go(at - 1); }
    else if (e.key === "Tab" && e.shiftKey) { e.preventDefault(); back(); }
  };
  return (
    <div className="eg-think" role="radiogroup" aria-label="思考强度" onKeyDown={onKey}>
      <span className="k" aria-hidden>思考</span>
      <div className="seg">
        {opts.map((l) => {
          const on = (l ?? undefined) === value;
          return (
            <button key={l ?? "auto"} type="button" role="radio" aria-checked={on} tabIndex={on ? 0 : -1} onClick={() => pick(l)}>
              {l ? LEVEL_NAME[l] || l : "自动"}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** A stage and the model it goes to. The list opens under the cell, with what each subscription has left beside its
 *  models; on a phone it comes up as a sheet. `none`: 不换 is one of the choices; `note`: a word under the name;
 *  `effort`: the cell also sets how hard its model thinks, under the list, for a model that thinks. */
export function RoutePicker({ value, models, video, name, bad, lefts, onPick, none = false, note, effort }: {
  value: string; models: any[]; video: boolean; name: string; bad: boolean;
  lefts: Record<string, Left | null>; onPick: (r: string) => void; none?: boolean; note?: string; effort?: Effort;
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
  const levels = effort ? levelsOf(value, models) : [];
  const level = shownLevel(value, models, effort?.value);
  const think = useRef<HTMLDivElement>(null);

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
    else if (e.key === "Tab" && !phone && !e.shiftKey && think.current) {
      e.preventDefault();
      think.current.querySelector<HTMLElement>('[aria-checked="true"]')?.focus();
    }
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
      {sections(value, models, video, lefts, none).map((s) => (
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
  const backToList = () => (pop.current || root.current)?.querySelector<HTMLElement>('[role="option"][aria-selected="true"], [role="option"]:not([aria-disabled="true"])')?.focus();
  const footer = levels.length > 0 && effort && (
    <div ref={think}>
      <Think levels={levels} value={level} back={backToList} pick={(l) => { if ((l ?? undefined) !== level) effort.onPick(l); }} />
    </div>
  );

  return (
    <div ref={root} className={`eg-route p-${familyOf(value, models)}${bad || gone ? " bad" : ""}${open ? " open" : ""}${value === NO_ROUTE ? " none" : ""}`}>
      <button ref={trigger} type="button" className="eg-route-b" aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id : undefined}
        aria-label={`${name}：${modelName(value, models)}${level ? `，思考${LEVEL_NAME[level] || level}` : ""}`} onClick={() => (open ? close() : setOpen(true))}
        onKeyDown={(e) => { if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); setOpen(true); } }}>
        <span className="eg-route-k">{name}{note && <small>{note}</small>}</span>
        <span className="eg-route-v">
          <span className="t">{modelName(value, models)}</span>
          {level && <span className="lv">{LEVEL_NAME[level] || level}</span>}
          {gone ? <em>已取消</em> : video && !gemini ? <em>抽帧</em> : null}
          <span className="chev" aria-hidden />
        </span>
      </button>
      {open && !phone && (
        <div ref={pop} className={`eg-pick${place?.up ? " up" : ""}${place?.end ? " end" : ""}${place ? " placed" : ""}`} style={place ? { maxHeight: place.h } : undefined}>
          {list}
          {footer}
        </div>
      )}
      {open && phone && <Sheet ref={sheet} title={name} onClose={() => close(false)}><div className="eg-pick-sheet">{list}{footer}</div></Sheet>}
    </div>
  );
}

/** 分工: which model each stage uses (stage_providers over llm_provider), from what magpie lists */
/** A cell's level of thinking: shown at once, sent after what was sent before it; one refused goes back */
function useEfforts(stored: Record<string, string>, save: Save) {
  const [picks, setPicks] = useState<Record<string, string | null>>({});
  useEffect(() => setPicks({}), [stored]);
  const sending = useRef<Promise<unknown>>(Promise.resolve());
  return (k: string): Effort => ({
    value: k in picks ? picks[k] : stored[k] ?? null,
    onPick: (l) => {
      setPicks((was) => ({ ...was, [k]: l }));
      sending.current = sending.current.then(async () => {
        if (!await save({ effort: { [k]: l } })) setPicks({});
      });
    },
  });
}

/** `own`: cells after 其余 that are settings of their own rather than stages (a channel's 底稿, knobs.CHANNEL_DRAFT) */
export function RouteCells({ kn, steps, own = [], label, save, models, routes, missing, lefts }: {
  kn: any; steps: string[]; own?: string[]; label: Record<string, string>; save: Save; models: any[];
  routes: { default: string; stages: Record<string, string> }; missing: string[]; lefts: Record<string, Left | null>;
}) {
  // a pick shows at once; the page's next read of the box replaces it, and one that was refused goes back
  const [picks, setPicks] = useState<Record<string, string>>({});
  useEffect(() => setPicks({}), [kn]);
  const sending = useRef<Promise<unknown>>(Promise.resolve());
  const main = picks.default || routes.default;
  const route = (k: string) => picks[k] || routes.stages[k] || main;
  const effort = useEfforts(kn.efforts || {}, save);
  const pick = (k: string, r: string) => {
    // 其余 going to a model that cannot watch: the stages that watch and had no route of their own stay where they were going
    const stay = k === "default" && models.find((m) => m.id === r)?.watches === false
      ? Object.fromEntries(steps.filter((s) => VIDEO_STEPS.has(s)).map((s) => [s, route(s)])) : {};
    setPicks((was) => ({ ...was, ...stay, [k]: r }));
    sending.current = sending.current.then(async () => {
      if (!await save(k === "default" ? { llm_provider: r } : own.includes(k) ? { [k]: r } : { stage_route: { [k]: r } })) setPicks({});
    });
  };
  return (
    <div className="eg-routes">
      {steps.map((k) => (
        <RoutePicker key={k} value={route(k)} models={models} video={VIDEO_STEPS.has(k)} name={label[k]}
          bad={missing.includes(route(k))} lefts={lefts} onPick={(r) => pick(k, r)} effort={effort(k)} />
      ))}
      <RoutePicker value={main} models={models} video={false} name="其余" bad={missing.includes(main)} lefts={lefts} onPick={(r) => pick("default", r)}
        effort={effort("default")} />
      {own.map((k) => (
        <RoutePicker key={k} value={route(k)} models={models} video={VIDEO_STEPS.has(k)} name={label[k]}
          bad={missing.includes(route(k))} lefts={lefts} onPick={(r) => pick(k, r)} effort={effort(k)} />
      ))}
    </div>
  );
}

/** 专题's four steps (knobs.TOPIC_STAGES), routed apart from production: 底稿 and 回看 watch the video, 复核 is shown the
 *  frames again (a model that takes images), 写作 plans, writes and puts the answer together. Picking a step's default
 *  route clears it (knobs.save keeps only what differs). */
export const TOPIC_STEPS = ["draft", "look", "second", "topic"] as const;
export const TOPIC_STEP: Record<string, string> = { draft: "底稿", look: "回看", second: "复核", topic: "写作" };
const TOPIC_VIDEO = new Set(["draft", "look"]);

export function TopicRouteCells({ routes, efforts, save, models, missing, lefts }: {
  routes: Record<string, string>; efforts: Record<string, string>; save: Save; models: any[]; missing: string[]; lefts: Record<string, Left | null>;
}) {
  const effort = useEfforts(efforts, save);
  const [picks, setPicks] = useState<Record<string, string>>({});
  useEffect(() => setPicks({}), [routes]);
  const sending = useRef<Promise<unknown>>(Promise.resolve());
  const seeing = models.filter((m) => m.vendor === "agy" || (m.inputs || []).includes("image"));
  const pick = (k: string, r: string) => {
    setPicks((was) => ({ ...was, [k]: r }));
    sending.current = sending.current.then(async () => {
      if (!await save({ topic_route: { [k]: r } })) setPicks({});
    });
  };
  return (
    <div className="eg-routes">
      {TOPIC_STEPS.map((k) => {
        const value = picks[k] || routes[k] || "agy";
        return (
          <RoutePicker key={k} value={value} models={k === "second" ? seeing : models} video={TOPIC_VIDEO.has(k)} name={TOPIC_STEP[k]}
            bad={missing.includes(value)} lefts={lefts} onPick={(r) => pick(k, r)} effort={effort(k)} />
        );
      })}
    </div>
  );
}

/** How many lines of 分工 go to a model that is no longer there */
export function lostRoutes(steps: string[], routes: { default: string; stages: Record<string, string> }, models: any[], missing: string[]): number {
  return [...steps.map((k) => routes.stages[k] || routes.default), routes.default].filter((r) => missing.includes(r) || withdrawn(r, models)).length;
}

/** 分工 as the 机房 shows it: who is doing each stage, and how each is doing. The choosing is on the 设置 page. */
export function RouteLinks({ steps, own = [], label, models, routes, missing, live, efforts = {} }: {
  steps: string[]; own?: string[]; label: Record<string, string>; models: any[];
  routes: { default: string; stages: Record<string, string> }; missing: string[]; live: string[]; efforts?: Record<string, string>;
}) {
  const cell = (k: string, name: string, value: string, video: boolean) => {
    const gone = withdrawn(value, models);
    const level = shownLevel(value, models, efforts[k]);
    return (
      <div key={k} className={`eg-route p-${familyOf(value, models)}${missing.includes(value) || gone ? " bad" : ""}${live.includes(k) ? " live" : ""}`}>
        <a className="eg-route-b" {...linkProps("/settings#models")} aria-label={`${name}：${modelName(value, models)}${level ? `，思考${LEVEL_NAME[level] || level}` : ""}`}>
          <span className="eg-route-k">{name}</span>
          <span className="eg-route-v">
            <span className="t">{modelName(value, models)}</span>
            {level && <span className="lv">{LEVEL_NAME[level] || level}</span>}
            {gone ? <em>已取消</em> : video && !isAgy(value) && value !== "tuzi" ? <em>抽帧</em> : null}
          </span>
        </a>
      </div>
    );
  };
  return (
    <div className="eg-routes">
      {steps.map((k) => cell(k, label[k], routes.stages[k] || routes.default, VIDEO_STEPS.has(k)))}
      {cell("default", "其余", routes.default, false)}
      {own.map((k) => cell(k, label[k], routes.stages[k] || routes.default, VIDEO_STEPS.has(k)))}
    </div>
  );
}
