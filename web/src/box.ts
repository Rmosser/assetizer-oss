/* The box's state in words: what the 机房 shows at a glance and the 设置 page shows beside each setting (`_box` in web/app.py). */
import type { GDriveView } from "./api";
import type { Left } from "./components/Routes";
import { ago, at8, BACKFILL_BAD, BACKFILL_STOP, bytes, VENDOR_NAME } from "./util";

export type Lamp = "ok" | "bad" | "idle" | "warn";
export type Line = { lamp: Lamp; value: string };
export type Bucket = { window: string; remaining: number | null; reset_at: string | null; disabled: boolean };
export type Group = { name: string; models: string; buckets: Bucket[] };

/** stage keys of model calls (providers.stage_key) */
export const STEP: Record<string, string> = { understanding: "理解", merge: "合并", knowledge: "知识", article: "文章", selection: "选片", boundary: "边界", review: "预审", compile: "编译", health: "探测", media: "自检",
  draft: "底稿", look: "回看", second: "复核", topic: "写作" };
export const MAIN_STEPS = ["understanding", "merge", "knowledge", "article", "selection", "boundary", "review"];

export const MAGPIE_DOWN: Record<string, string> = { UNREACHABLE: "连不上 magpie", BAD_KEY: "Key 失效", HELD: "这个 Key 读不到额度", NO_KEY: "没有放 Key", REFUSED: "magpie 拒收" };
export const subName = (s: any) => VENDOR_NAME[s.provider] || s.name || s.provider;
/** a ChatGPT account whose window is used up goes on, the vendor spending its credits (magpie_state._credits) */
export const onCredits = (s: any) => s.credits > 0 && ((s.windows || []) as Bucket[]).some((b) => !b.disabled && b.remaining === 0);
export const creditText = (n: number) => (n >= 100 ? Math.round(n) : n).toLocaleString("en-US");
export const subFamily = (s: any) => ({ codex: "chatgpt", claude: "claude", grok: "grok" } as Record<string, string>)[s.provider] || "other";

const WEEKDAY = "日一二三四五六";
/** when a window starts over: within a day "2 小时 13 分后", further "周五 07:34"; it 重置 — or, used up, 恢复 */
export function resets(iso?: string | null, weekly = false, out = false): string | null {
  if (!iso) return null;
  const t = new Date(iso), s = (t.getTime() - Date.now()) / 1000;
  const verb = out ? "恢复" : "重置";
  if (Number.isNaN(s)) return null;
  if (s <= 0) return `已到${verb}时间`;
  const hm = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  if (weekly || s > 86400) return `${s > 6 * 86400 ? `${t.getMonth() + 1}月${t.getDate()}日` : `周${WEEKDAY[t.getDay()]}`} ${hm} ${verb}`;
  const all = Math.max(1, Math.round(s / 60)), h = Math.floor(all / 60), m = all % 60;  // 7199 s: 2 小时 0 分, not 1 小时 60 分
  return `${h ? `${h} 小时 ` : ""}${m} 分后${verb}`;
}

/** The groups of agy's quota the stages draw on, the default's first: agy keeps one per group of models, as its
 *  `/usage` groups them, so two stages on Gemini may draw on two */
export const agyGroups = (q: any): Group[] => ((q?.in_use || []) as string[]).flatMap((n) => ((q?.groups || []) as Group[]).filter((g) => g.name === n));

/** A group of agy's quota by name: its main one, "Gemini Models", is plain Gemini */
export const groupName = (g?: Group) => g?.name.replace(/\s+models?$/i, "") || "Gemini";

/** What each subscription has left, for whoever is picking a model: the tightest window, or what stands in its way. A
 *  Gemini model's is the group of agy's quota it draws on (`quota`, set by the box); magpie's, by subscription. */
export function quotaLefts(data: any): Record<string, Left | null> {
  const q = data.quota?.agy, st = data.magpie?.state || {};
  const down = st.error === "UNREACHABLE" || st.error === "BAD_KEY";
  const leftOf = (ws: any[] = []): Left | null => {
    const xs = ws.filter((b) => !b.disabled && b.remaining != null);
    if (!xs.length) return null;
    const low = xs.reduce((a, b) => (b.remaining < a.remaining ? b : a));
    return low.remaining === 0 ? { note: resets(low.reset_at, low.window === "weekly", true) || "用完了", bad: true } : { left: low.remaining };
  };
  const lefts: Record<string, Left | null> = {};
  for (const m of data.agy_models || []) {
    const g = ((q?.groups || []) as Group[]).find((x) => x.name === m.quota);
    lefts[m.id] = q?.error === "AGY_AUTH_REQUIRED" ? { note: "没有登录", bad: true } : q?.ok && g ? leftOf(g.buckets) : null;
  }
  for (const s of st.subs || []) {
    const l: Left | null = s.signed_out ? { note: "要重新登录", bad: true } : s.error ? { note: "读不到额度" } : down ? null : onCredits(s) ? { note: "用积分" } : leftOf(s.windows);
    const was = lefts[s.provider];
    if (!was || was.left == null || (l?.left ?? 0) > was.left) lefts[s.provider] = l;  // several accounts: the gateway can use the one with the most
  }
  return lefts;
}

/** 到 15:00, 到明天 07:00 */
export const till = (iso: string) => { const at = at8(iso); return /^\d+:/.test(at) ? `到 ${at}` : `到${at}`; };

/** What 补录 does now, in a few words (ingest.backfill_view). */
export function paceText(v: any): string {
  if (v.active > v.slots) return `收尾 ${v.active} 期 · 之后 ${v.slots} 路`;
  if (v.fast) return v.until ? `${v.slots} 路 · ${till(v.until)}` : `${v.slots} 路`;
  return v.next ? `1 路 · ${at8(v.next)} 起 ${v.lanes} 路` : "1 路";
}

/** The lamp a reason 补录 is held up by wears: the ones that need the user, the ones that pass by themselves, the ordinary */
export const backfillStopLamp = (stop: string): Lamp => (BACKFILL_BAD.includes(stop) ? "bad" : stop === "queued" || stop === "trial" ? "ok" : "warn");

function backfillLine(v: any): Line {
  if (!v.pending && !v.active) return { lamp: "idle", value: "没有排队" };
  if (!v.pending) return { lamp: "ok", value: `收尾 ${v.active} 期` };
  const n = `${v.pending} 期`;
  if (v.stop) return { lamp: backfillStopLamp(v.stop), value: `${n} · ${BACKFILL_STOP[v.stop] || v.stop}` };
  return { lamp: "ok", value: v.active > v.slots ? `${n} · 收尾中` : `${n} · ${v.slots} 路` };
}

const ASR_CODE: Record<string, string> = { UNREACHABLE: "连不上", BAD_TOKEN: "令牌不对" };
export const asrCode = (code: string) => ASR_CODE[code] || code;

/** 转写: no address → CPU; the last test failing → what failed; else the device the last issue used */
function asrLine(a: any): Line {
  if (!a.url) return { lamp: "idle", value: "CPU" };
  if (a.test && !a.test.ok) return { lamp: "bad", value: asrCode(a.test.code) };
  if (a.last?.fell_back) return { lamp: "idle", value: "退回 CPU" };
  return { lamp: "ok", value: "GPU" };
}

/** CookieCloud as the last read found it: `down`, the cause to show rather than the 需要登录 it leads to */
export function cookieState(y: any) {
  const cc = y.cookiecloud;
  const read = y.state?.source === "cookiecloud" ? y.state : null;
  return { cc, read, down: !!(cc?.configured && read && !read.ok), kept: y.kept_at as string | null };
}

function youtubeLine(data: any): Line {
  const y = data.youtube;
  const need = data.alerts.some((a: any) => a.key === "YOUTUBE_AUTH" && !a.resolved_at);
  const { cc, read, down, kept } = cookieState(y);  // kept: the last login that worked, gone on with while CookieCloud does not answer
  if (down) return !kept ? { lamp: "bad", value: "读不到 CookieCloud" } : need ? { lamp: "bad", value: "上一份也失效了" } : { lamp: "warn", value: `用上一份 · ${ago(kept)}` };
  if (need) return { lamp: "bad", value: "需要登录" };
  if (cc?.configured) return read?.ok ? { lamp: "ok", value: "已就绪" } : { lamp: "idle", value: "待验证" };
  return y.profile_ok ? { lamp: "ok", value: "已就绪" } : { lamp: "idle", value: "未配置" };
}

function magpieLine(m: any): Line {
  const st = m.state || {};
  const offline = (st.subs || []).some((s: any) => s.signed_out && (m.vendors || []).includes(s.provider));
  return st.error === "NO_KEY" ? { lamp: "idle", value: "未配置" } : st.error === "UNREACHABLE" ? { lamp: "bad", value: "连不上" }
    : st.error === "BAD_KEY" ? { lamp: "bad", value: "Key 失效" } : st.error === "HELD" ? { lamp: "warn", value: "Key 受限" }
    : offline ? { lamp: "bad", value: "账号掉线" } : st.ok ? { lamp: "ok", value: "已连接" } : { lamp: "idle", value: "读取中" };
}

function agyLine(data: any): Line {
  const q = data.quota?.agy;
  const used = (data.providers?.in_use || []).includes("agy");
  if (q?.error === "AGY_AUTH_REQUIRED") return { lamp: used ? "bad" : "idle", value: "未登录" };
  if (used && data.agy_media?.ok === false) return { lamp: "bad", value: "读不了视频" };
  if (q?.ok) return { lamp: "ok", value: "已登录" };
  if (q) return { lamp: used ? "warn" : "idle", value: "读取失败" };
  return { lamp: "idle", value: used ? "读取中" : "不在用" };
}

function telegramLine(tg: any): Line {
  if (!(tg.token && tg.chat)) return { lamp: "idle", value: tg.token ? "待接通" : "未接" };
  if (tg.pending) return { lamp: "bad", value: `${tg.pending} 条没发出去` };
  return tg.ok === false ? { lamp: "bad", value: "发不出去" } : { lamp: "ok", value: "已接通" };
}

/** authors and topics whose 传 / 不传 is not decided yet: "3 位待定", "1 个专题待定", "3 位 · 1 个专题待定" */
export function pendingRules(g?: GDriveView | null): string | null {
  const a = g?.rules?.authors.pending || 0, t = g?.rules?.topics.pending || 0;
  return a || t ? `${[a && `${a} 位`, t && `${t} 个专题`].filter(Boolean).join(" · ")}待定` : null;
}

/** 网盘备份, on the 设置 page only (it does not hold production up): what is backed up, or what stands in the way */
export function gdriveLine(g?: GDriveView | null): Line | undefined {
  if (!g) return undefined;
  const line = ((): Line => {
    switch (g.state) {
      case "ok": return { lamp: "ok", value: g.usage?.videos ? `${g.usage.videos} 期 · ${bytes(g.usage.bytes)}` : "已就绪" };
      case "auth": return { lamp: "bad", value: "登录失效" };
      case "failing": return { lamp: "bad", value: "上传失败" };
      case "login": return { lamp: "idle", value: "未登录" };
      case "folder": return { lamp: "idle", value: g.folder ? "待测试" : "待建文件夹" };
      default: return { lamp: "idle", value: "未配置" };
    }
  })();
  // what is not decided is said in amber once everything else works: a step still to do, or one gone wrong, comes first
  const pending = g.state === "ok" ? pendingRules(g) : null;
  return pending ? { lamp: "warn", value: pending } : line;
}

/** How each part of the box is doing: a lamp and a few words, the same on both pages. */
export function boxLines(data: any): Record<"agy" | "magpie" | "youtube" | "notion" | "disk" | "slots" | "backfill" | "asr" | "notify", Line> {
  const n = data.notion, d = data.disk, kn = data.knobs;
  return {
    agy: agyLine(data),
    magpie: magpieLine(data.magpie || {}),
    youtube: youtubeLine(data),
    notion: { lamp: n.token && (n.database || n.parent) ? "ok" : "idle", value: n.token ? (n.database ? "已连接" : "待指定位置") : "未配置" },
    disk: { lamp: d.free / d.total < 0.15 ? "bad" : "ok", value: `${bytes(d.free)} 可用` },
    slots: { lamp: "ok", value: `印 ${kn.worker_slots} · 补录 ${data.backfill.slots} · 下载 ${kn.download_slots}` },
    backfill: backfillLine(data.backfill),
    asr: asrLine(data.asr),
    notify: telegramLine(data.telegram),
  };
}

/** Where an alert is fixed, when that is a part of the 设置 page */
export const ALERT_FIX: Record<string, { href: string; label: string }> = {
  YOUTUBE_AUTH: { href: "/settings#cc", label: "配置与测试" },
  YOUTUBE_REFUSED: { href: "/settings#cc", label: "配置与测试" },
  cookiecloud: { href: "/settings#cc", label: "配置与测试" },
  PROVIDER_MODEL: { href: "/settings#models", label: "换模型" },
  DISK_LOW: { href: "/settings#disk", label: "看磁盘" },
};

/** Where an alert is fixed: what the box raised it with (`action` {label, href}, a page of this app), else ALERT_FIX */
export function alertFix(a: { key: string; action?: unknown }): { href: string; label: string } | undefined {
  let x: any = a.action;
  if (typeof x === "string") { try { x = JSON.parse(x); } catch { x = null; } }
  if (x && typeof x.href === "string" && /^\/(?![\/\\])/.test(x.href) && typeof x.label === "string" && x.label) return { href: x.href, label: x.label };
  return ALERT_FIX[a.key];
}
