export class AuthExpired extends Error {
  constructor() { super("访问凭证已过期"); this.name = "AuthExpired"; }
}

async function parse(r: Response) {
  const type = r.headers.get("content-type") || "";
  // Cloudflare Access answers an expired session with its login page (HTML), not with JSON
  if (r.status === 401 || r.status === 403 || (r.ok && !type.includes("json"))) throw new AuthExpired();
  return r;
}

/** An error answer, with its status: 409 says the page asked about a state that has moved on (another device). */
export class HttpError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

export async function get<T = any>(path: string): Promise<T> {
  const r = await parse(await fetch(path, { headers: { Accept: "application/json" } }));
  if (!r.ok) {
    // FastAPI errors are {"detail": "..."}: show the sentence, not the JSON
    const t = await r.text();
    let d = t;
    try { d = JSON.parse(t).detail || t; } catch { /* not JSON */ }
    throw new HttpError(typeof d === "string" ? d : `${r.status} ${t}`, r.status);
  }
  return r.json();
}

async function send<T>(method: "POST" | "PUT", path: string, body: unknown): Promise<T> {
  const r = await parse(await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new HttpError((data as any).detail || r.statusText, r.status);
  return data as T;
}

export const post = <T = any>(path: string, body: unknown = {}) => send<T>("POST", path, body);
export const put = <T = any>(path: string, body: unknown = {}) => send<T>("PUT", path, body);

export const media = (id: number | null | undefined, w?: number) => (id ? `/media/${id}${w ? `?w=${w}` : ""}` : undefined);
/** responsive stills: the server keeps 320–1280 px JPEG derivatives of screenshots and posters */
export const srcSet = (id: number | null | undefined, widths = [640, 1280]) =>
  id ? widths.map((w) => `/media/${id}?w=${w} ${w}w`).join(", ") : undefined;

/* 网盘备份: a topic's originals and drafts copied to the user's own Google Drive (rclone). The box tells whether a client
   and a sign-in are there, never what they are: no ID, secret or token reaches the page. */
export type GDriveState = "off" | "client" | "login" | "folder" | "ok" | "auth" | "failing";
export type GDriveView = {
  rclone: boolean; client: boolean; token: boolean;
  folder: { id: string; path: string; url: string } | null;
  /** off: no rclone; client: no client; login: not signed in; folder: no folder; auth: the sign-in gone bad; failing: the last upload failed */
  state: GDriveState;
  error: string | null;
  login: { pending: boolean; url: string; expires_at: string } | null;
  usage: { videos: number; bytes: number; drive_free: number | null; drive_total: number | null; checked_at: string } | null;
  queue: { due: number; failed: number; missed: number };
  /** the video being uploaded; its title when the box knows it */
  uploading: { id: string; title: string | null } | null;
  /** how many authors and topics are 传 / 不传 / not decided yet; null until the folder is made */
  rules?: { authors: RuleCount; topics: RuleCount } | null;
  /** copies in the folder that no rule wants any more */
  deletable?: Size | null;
  /** a batch being moved to the drive's trash, one video at a time */
  deleting?: { total: number; done: number } | null;
};
export type Size = { videos: number; bytes: number };
export type RuleCount = { on: number; off: number; pending: number };
/** 传 / 不传; null: not decided yet (待定) */
export type Pass = boolean | null;
/** An author is known by the YouTube channel ID, never by name; `channel`: it is one of the 频道 */
export type AuthorRule = {
  id: string; name: string; channel: { id: string; name: string } | null; member: Pass; public: Pass;
  /** its videos here (topics and the channel's issues, each video once) */
  videos: number;
  /** its checked copies in the current folder */
  drive: Size;
};
export type TopicRule = { id: number; question: string; backup: Pass; videos: number; drive: Size };
export type Rules = { ready: boolean; authors?: AuthorRule[]; topics?: TopicRule[] };
export type RulesChange = { authors?: { id: string; member?: boolean; public?: boolean }[]; topics?: { id: number; backup: boolean }[] };
export type DriveCopy = { id: string; title: string | null; bytes: number };
/** What can go to the drive's trash: by author; `last_copy`: gone from YouTube and from the box, the only copy left;
 *  `kept`: copies a rule set to 传 keeps, by the rule that keeps them */
export type Deletable = {
  ready: boolean;
  authors?: { id: string; name: string; bytes: number; videos: DriveCopy[] }[];
  last_copy?: (DriveCopy & { author: { id: string; name: string } | null })[];
  kept?: ({ topic: { id: number; question: string }; videos: number } | { author: { id: string; name: string }; videos: number })[];
  total?: Size;
};
/** a channel's author, as the 定稿单 and the 看板 set it; not ready: no drive set up, or the channel has no YouTube ID.
 *  `drive`: all its copies on the drive; `deletable`: those of them no rule wants any more */
export type ChannelBackup = { ready: boolean; author?: { id: string; name: string } | null; member?: Pass; public?: Pass; drive?: Size; deletable?: Size };
/** One video of a topic: how its backup (original and draft) stands; `restored`: its original was fetched back from the
 *  drive (a draft fetched back alone does not count) */
export type ItemBackup = { state: "done" | "due" | "failed" | "missed" | null; error: string | null; restored: boolean };
/** A topic's videos whose backup failed (original or draft) or that were cleared before it; `auth`: the drive's sign-in
 *  has gone bad, so trying again will not help. null: no backup set up */
export type TopicBackup = { failed: number; missed: number; error: string | null; auth: boolean } | null;

export const gdrive = {
  client: (client_id: string, client_secret: string) => post<GDriveView>("/api/gdrive/client", { client_id, client_secret }),
  clearClient: () => post<GDriveView>("/api/gdrive/client", { clear: ["client"] }),
  login: () => post<GDriveView>("/api/gdrive/login"),
  /** what the user pasted back: the whole http://127.0.0.1:53682/?state=…&code=… or the code alone */
  code: (url: string) => post<GDriveView>("/api/gdrive/login/code", { url }),
  cancel: () => post<GDriveView>("/api/gdrive/login/cancel"),
  folder: (path: string) => post<GDriveView>("/api/gdrive/folder", { path }),
  test: () => post<GDriveView>("/api/gdrive/test"),
  disconnect: () => post<GDriveView>("/api/gdrive/disconnect"),
  /** a topic's videos that did not make it, queued again */
  retry: (tid: number) => post<{ ok: boolean; due: number }>(`/api/topics/${tid}/backup/retry`),
  ready: () => get<{ ready: boolean }>("/api/gdrive/ready"),
  rules: () => get<Rules>("/api/gdrive/rules"),
  /** only 传 or 不传: what is decided is not undecided again */
  setRules: (change: RulesChange) => put<Rules>("/api/gdrive/rules", change),
  deletable: () => get<Deletable>("/api/gdrive/deletable"),
  /** moved to the drive's trash in the background (202); 409 while another batch is being moved */
  trash: (videos: string[]) => post<GDriveView>("/api/gdrive/delete", { videos }),
  channel: (cid: string) => get<ChannelBackup>(`/api/channels/${encodeURIComponent(cid)}/backup`),
  setChannel: (cid: string, change: { member?: boolean; public?: boolean }) => put<ChannelBackup>(`/api/channels/${encodeURIComponent(cid)}/backup`, change),
};
