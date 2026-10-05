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
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export async function post<T = any>(path: string, body: unknown = {}): Promise<T> {
  const r = await parse(await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((data as any).detail || r.statusText);
  return data as T;
}

export const media = (id: number | null | undefined) => (id ? `/media/${id}` : undefined);
