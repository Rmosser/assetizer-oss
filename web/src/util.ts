const CN = "〇一二三四五六七八九";

export function cnYear(y: number) {
  return String(y).split("").map((d) => CN[+d]).join("");
}

export function cnNum(n: number): string {
  if (n <= 10) return n === 10 ? "十" : CN[n];
  if (n < 20) return "十" + CN[n - 10];
  const t = Math.floor(n / 10), o = n % 10;
  return CN[t] + "十" + (o ? CN[o] : "");
}

const WEEK = ["日", "一", "二", "三", "四", "五", "六"];

export function cnDate(d = new Date()) {
  return { md: `${cnNum(d.getMonth() + 1)}月${cnNum(d.getDate())}日`, y: `${cnYear(d.getFullYear())}年`, w: `星期${WEEK[d.getDay()]}` };
}

/** "20260908" | ISO → Date (local) */
export function parseDay(s?: string | null): Date | null {
  if (!s) return null;
  if (/^\d{8}$/.test(s)) return new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8));
  const d = new Date(s);
  return isNaN(+d) ? null : d;
}

/** "20260908" → 九月八日 */
export function cnDay(s?: string | null) {
  const d = parseDay(s);
  return d ? `${cnNum(d.getMonth() + 1)}月${cnNum(d.getDate())}日` : "";
}

/** group header for the archive: 二〇二六年 · 九月 */
export function cnMonth(d: Date) {
  return `${cnYear(d.getFullYear())}年 · ${cnNum(d.getMonth() + 1)}月`;
}

/** 3504 s → 58′ */
export const mins = (sec?: number | null) => (sec ? `${Math.round(sec / 60)}′` : "—");

export function tc(sec?: number | null, withHours = true) {
  if (sec == null || isNaN(sec)) return "—";
  sec = Math.max(0, sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h || withHours ? `${h ? String(h).padStart(2, "0") + ":" : ""}${mm}:${ss}` : `${mm}:${ss}`;
}

export function dur(sec?: number | null) {
  if (!sec) return "—";
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : `${m} 分钟`;
}

export function pubDate(s?: string | null) {
  if (!s) return "";
  if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}·${s.slice(4, 6)}·${s.slice(6, 8)}`;
  return s.slice(0, 10).replaceAll("-", "·");
}

export function ago(iso?: string | null) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

export function bytes(n?: number | null) {
  if (!n) return "0";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

export const PRODUCTION: Record<string, string> = {
  QUEUED: "排队中", RUNNING: "付印中", WAITING: "等待中", INTERRUPTED: "已中断", FAILED: "失败",
  COMPLETE: "已完成", COMPLETE_PARTIAL: "完成 · 有告警", CANCELLED: "已取消", EXPERIMENT: "实验",
};
export const WAIT: Record<string, string> = {
  AGY_AUTH_REQUIRED: "模型登录失效", PROVIDER_QUOTA: "模型配额", PROVIDER_MODEL: "工序的模型已取消", YOUTUBE_AUTH: "YouTube 需登录",
  DISK_LOW: "磁盘不足", PLAYBOOK_CONFIRMATION: "频道待定稿", LIVE_IN_PROGRESS: "直播未结束",
  AGY_MEDIA_READ: "agy 读不了视频", NETWORK: "网络中断，稍后重试", YTDLP_OUTDATED: "下载工具需更新",
  YOUTUBE_REFUSED: "YouTube 拒绝下载",
};
/** why a run failed, in a few words */
export const ERRORS: Record<string, string> = {
  PROVIDER_TIMEOUT: "模型超时", PROVIDER_FAILURE: "模型出错", PROVIDER_TRANSIENT: "模型暂时不可用", PROVIDER_OUTPUT_LIMIT: "输出超长",
  CONTENT_SAFETY: "被内容安全过滤拦下", MODALITY_ROUTING_DEVIATION: "模型越界读文件", MEDIA_READ_FAILED: "读不了视频", CONTRACT: "输出不合规", DOWNLOAD: "下载失败",
  TOO_MANY_RESTARTS: "反复重启", VIDEO_REMOVED: "视频已删除", VIDEO_PRIVATE: "视频设为私享", VIDEO_MEMBERS: "会员限定视频",
  SOURCE_UNAVAILABLE: "视频看不了", STOPPED: "清空时停下", "?": "未分类",
};
/** what stands in the way of 补录 (ingest.backfill_stop) */
export const BACKFILL_STOP: Record<string, string> = {
  paused: "队列暂停中", queued: "先印新视频", trial: "先跑试跑", weekly: "周额度留给新视频", "5h": "5 小时额度留给新视频",
  YOUTUBE_AUTH: "等 YouTube 登录", YOUTUBE_REFUSED: "YouTube 拒绝下载", DISK_LOW: "磁盘不足", YTDLP_OUTDATED: "下载工具需更新",
};
/** those that need the user; the others pass by themselves */
export const BACKFILL_BAD = ["YOUTUBE_AUTH", "YOUTUBE_REFUSED", "DISK_LOW", "YTDLP_OUTDATED"];

/** The open alerts as they are shown: CookieCloud not answering and the login it costs are one thing to fix. */
export function mergeAlerts(alerts: any[]): any[] {
  const cc = alerts.find((a) => a.key === "cookiecloud"), auth = alerts.find((a) => a.key === "YOUTUBE_AUTH");
  if (!cc || !auth) return alerts;
  return alerts.filter((a) => a !== cc).map((a) => (a === auth ? { ...a, title: "读不到 CookieCloud，要登录的视频在等", body: cc.body } : a));
}

const HOUR = 3600e3;
const EAST8 = 8 * HOUR;  // the box's clock (notify.TZ), whatever the browser is set to
const hh = (n: number) => String(n).padStart(2, "0");
/** the box's hour and minute now */
export function hourMinute8(): [number, number] {
  const d = new Date(Date.now() + EAST8);
  return [d.getUTCHours(), d.getUTCMinutes()];
}
/** when, on the box's clock: "09:00", "明天 09:00", "后天 09:00", "11月9日 09:00" */
export function at8(iso: string): string {
  const d = new Date(+new Date(iso) + EAST8);
  const days = Math.floor(+d / (24 * HOUR)) - Math.floor((Date.now() + EAST8) / (24 * HOUR));
  const hm = `${hh(d.getUTCHours())}:${hh(d.getUTCMinutes())}`;
  return days <= 0 ? hm : days === 1 ? `明天 ${hm}` : days === 2 ? `后天 ${hm}` : `${d.getUTCMonth() + 1}月${d.getUTCDate()}日 ${hm}`;
}
/** the nth whole hour from now, as the box takes a time */
export function nextHour(n: number): string {
  const t = (Math.floor(Date.now() / HOUR) + n) * HOUR;
  return new Date(t).toISOString().replace(/\.\d+Z$/, "+00:00");
}
/** [23, 0, 1, 13] → "13:00–14:00、23:00–02:00": stretches of hours, read across midnight (knobs.hours_text) */
export function hoursText(hours: number[]): string {
  const on = new Set(hours);
  if (on.size === 24) return "全天";
  const out: string[] = [];
  for (const h of [...on].sort((a, b) => a - b)) {
    if (on.has((h + 23) % 24)) continue;
    let end = h;
    while (on.has((end + 1) % 24)) end = (end + 1) % 24;
    out.push(`${hh(h)}:00–${hh((end + 1) % 24)}:00`);
  }
  return out.join("、");
}
/** a video no login opens: why, for 视频不可用 (a15) */
export const UNAVAILABLE: Record<string, string> = {
  VIDEO_REMOVED: "已删除", VIDEO_PRIVATE: "私享", VIDEO_MEMBERS: "会员限定", SOURCE_UNAVAILABLE: "看不了",
};
/** the waits that stop production (vermilion); the others pass by themselves (a live still on air, the network) */
export const STOPS = ["AGY_AUTH_REQUIRED", "PROVIDER_QUOTA", "PROVIDER_MODEL", "YOUTUBE_AUTH", "YOUTUBE_REFUSED", "DISK_LOW", "AGY_MEDIA_READ", "YTDLP_OUTDATED"];
export const DELIVERY: Record<string, string> = {
  NOT_STARTED: "待交付", BLOCKED: "Notion 未配置", DELIVERING: "交付中", DELIVERED: "已入 Notion",
  STALE: "待更新", FAILED: "交付失败", REGISTERED: "库里已有", HELD: "等跑完",
};
/** why a delivery is held, when it is not for a run to end: a partial version (a08), a page in the library it may be
 *  (a05), what is yours on the page it would replace (a07) */
export const HELD: Record<string, string> = { PARTIAL: "待补齐", MAYBE_HAVE: "待确认", YOURS_ON_PAGE: "待确认" };
/** a delivery state in words */
export const deliveryLabel = (d: string, reason?: string | null) => (d === "HELD" && reason && HELD[reason]) || DELIVERY[d] || d;

export function clock(iso?: string | null) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
/** For scrollTo / scrollIntoView: the CSS reduced-motion rule does not reach script-driven scrolling. */
export const smooth = (): ScrollBehavior => (reduceMotion() ? "auto" : "smooth");
