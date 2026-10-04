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
  return { md: `${cnNum(d.getMonth() + 1)}月${cnNum(d.getDate())}日`, y: `${cnYear(d.getFullYear())} · 星期${WEEK[d.getDay()]}` };
}

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

export const REVIEW: Record<string, string> = {
  UNREVIEWED: "待审阅", IN_REVIEW: "审阅中", APPROVED: "已通过", CHANGES_REQUESTED: "需修改", REJECTED: "已驳回",
};
export const PRODUCTION: Record<string, string> = {
  QUEUED: "排队中", RUNNING: "付印中", WAITING: "等待中", INTERRUPTED: "已中断", FAILED: "失败",
  COMPLETE: "已完成", COMPLETE_PARTIAL: "已完成（部分告警）", CANCELLED: "已取消",
};
export const WAIT: Record<string, string> = {
  AGY_AUTH_REQUIRED: "模型登录失效", PROVIDER_QUOTA: "模型配额", YOUTUBE_AUTH: "YouTube 需登录",
  DISK_LOW: "磁盘不足", PLAYBOOK_CONFIRMATION: "Playbook 待确认", LIVE_IN_PROGRESS: "直播未结束",
};
export const DELIVERY: Record<string, string> = {
  NOT_STARTED: "待交付", BLOCKED: "Notion 未配置", DELIVERING: "交付中", DELIVERED: "已交付 Notion",
  STALE: "待更新", FAILED: "交付失败",
};
