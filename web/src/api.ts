export class AuthExpired extends Error {
  constructor() { super("访问凭证已过期"); this.name = "AuthExpired"; }
}

async function parse(r: Response) {
  const type = r.headers.get("content-type") || "";
  // Cloudflare Access answers an expired session with its login page (HTML), not with JSON
  if (r.status === 401 || r.status === 403 || (r.ok && !type.includes("json"))) throw new AuthExpired();
  return r;
}

export async function get<T = any>(path: string): Promise<T> {
  const r = await parse(await fetch(path, { headers: { Accept: "application/json" } }));
  if (!r.ok) {
    // FastAPI errors are {"detail": "..."}: show the sentence, not the JSON
    const t = await r.text();
    let d = t;
    try { d = JSON.parse(t).detail || t; } catch { /* not JSON */ }
    throw new Error(typeof d === "string" ? d : `${r.status} ${t}`);
  }
  return r.json();
}

/** An error answer, with its status: 409 says the page asked about a state that has moved on (another device). */
export class HttpError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

export async function post<T = any>(path: string, body: unknown = {}): Promise<T> {
  const r = await parse(await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new HttpError((data as any).detail || r.statusText, r.status);
  return data as T;
}

export const media = (id: number | null | undefined, w?: number) => (id ? `/media/${id}${w ? `?w=${w}` : ""}` : undefined);
/** responsive stills: the server keeps 320–1280 px JPEG derivatives of screenshots and posters */
export const srcSet = (id: number | null | undefined, widths = [640, 1280]) =>
  id ? widths.map((w) => `/media/${id}?w=${w} ${w}w`).join(", ") : undefined;
