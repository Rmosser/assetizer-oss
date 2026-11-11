import { Fragment, useEffect, useState } from "react";
import { gdrive, type GDriveView, type RuleCount } from "../api";
import { bytes } from "../util";
import { AuthorsSheet, TopicsSheet, TrashSheet } from "./GDriveRules";
import { Clear, Row, type Said, Secret } from "./Knobs";

/* 设置 → 网盘备份: a topic's originals and drafts copied to the user's own Google Drive. The same eight rows always, in the
   same order — the user's own Google client (write-only, like the other keys), signing in at Google (open the page, paste
   back where it ends up), a folder, which authors and which topics go (GDriveRules.tsx), what is backed up, what waits,
   what no rule wants any more — a step not reached yet is a dash, so nothing comes and goes while it is set up, while it
   uploads or while it fails (2026-11-10, 11-11). The trouble is said once, in vermilion; what goes well, in numbers. */

const FOLDER = "课代表/专题备份";
const CLIENTS = "https://console.cloud.google.com/auth/clients";
const none = <span className="muted">—</span>;

/** the address that opens 可删除 from elsewhere (the 看板's 去删除); the page then stands at 网盘备份 */
export const TRASH_HASH = "#gdrive-delete";

/** a sign-in still waiting for what Google hands back; one past its time is over, whatever the last answer said */
const waiting = (g: GDriveView) => !!g.login?.pending && !!g.login.url && !(+new Date(g.login.expires_at) <= Date.now());

/** "5 位传 · 2 位不传 · 3 位待定", the numbers that are not zero ("0 位" when all are); what is not decided in amber */
function Tally({ c, unit }: { c: RuleCount; unit: string }) {
  const parts = [c.on > 0 && `${c.on} ${unit}传`, c.off > 0 && `${c.off} ${unit}不传`, c.pending > 0 && <span key="p" className="warn">{c.pending} {unit}待定</span>].filter(Boolean);
  return <span>{parts.length ? parts.map((p, i) => <Fragment key={i}>{i > 0 && " · "}<span className="nw">{p}</span></Fragment>) : `0 ${unit}`}</span>;
}

export function GDriveRows({ g: stored, reload, say }: { g: GDriveView; reload: () => void; say: (s: Said) => void }) {
  // what a step answered, until the page reads the box again
  const [mine, setMine] = useState<GDriveView | null>(null);
  useEffect(() => setMine(null), [stored]);
  const g = mine || stored;
  const [id, setId] = useState("");
  const [secret, setSecret] = useState("");
  const [back, setBack] = useState("");
  const [path, setPath] = useState(FOLDER);
  const [busy, setBusy] = useState(false);
  const [sheet, setSheet] = useState<"authors" | "topics" | "trash" | null>(null);
  // a batch going to the trash: how far it has got, read more often than the page's own round and, like it, not while
  // the page is out of sight (the page reads it again the moment it is back)
  const moving = !!g.deleting;
  useEffect(() => {
    if (!moving) return;
    const t = window.setInterval(() => { if (!document.hidden) reload(); }, 3000);
    return () => clearInterval(t);
  }, [moving, reload]);
  // rules can be set once signed in (a failing or lapsed sign-in keeps them); copies taken away only while it works
  const canSet = g.state === "ok" || g.state === "failing" || g.state === "auth";
  const canTrash = g.state === "ok" || g.state === "failing";
  const trashable = canTrash && !g.deleting && !!g.deletable?.videos;
  // come to take copies away (去删除 on a 看板): the sheet opens when there is something to take, else the row says why not
  useEffect(() => {
    const on = () => {
      if (location.hash !== TRASH_HASH) return;
      history.replaceState(history.state, "", "#gdrive");
      if (trashable) setSheet("trash");
    };
    on();
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, [trashable]);

  const act = async (call: () => Promise<GDriveView>, ok?: (v: GDriveView) => Said | null, done?: () => void) => {
    setBusy(true);
    try {
      const v = await call();
      setMine(v);
      done?.();
      reload();
      const s = ok?.(v);
      if (s) say(s);
    } catch (e) { say({ text: (e as Error).message, bad: true }); }
    setBusy(false);
  };

  if (g.state === "off" || !g.rclone) return <Row name="rclone">没装</Row>;

  const both = !!(id.trim() && secret.trim());
  const signIn = g.client && (g.state === "login" || g.state === "auth");
  const pending = signIn && waiting(g);
  const u = g.usage, q = g.queue || { due: 0, failed: 0, missed: 0 };
  const rules = g.rules, del = g.deletable, dl = g.deleting;
  const making = g.state === "folder" && !g.folder;
  const off = <Clear label="断开" sure="确认断开" disabled={busy} onClear={() => act(gdrive.disconnect, () => ({ text: "已断开" }))} />;
  const set = (k: "authors" | "topics") => canSet && <button type="button" className="btn sm" onClick={() => setSheet(k)}>设置</button>;
  // a change that could not be saved once its sheet was closed: said where the page says what it saved
  const lost = (text: string) => say({ text, bad: true });
  const login = (
    <button type="button" className="btn sm solid" disabled={busy} onClick={() => act(gdrive.login)}>{g.state === "auth" ? "重新登录" : "登录 Google"}</button>
  );
  return (
    <>
      {g.error && <p className="st-fail">{g.error}</p>}
      {g.client ? (
        <Row name="客户端">
          <span>已填</span>
          <Clear disabled={busy} onClear={() => act(gdrive.clearClient, () => ({ text: "已清除" }))} />
        </Row>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); if (both && !busy) act(() => gdrive.client(id.trim(), secret.trim()), () => ({ text: "已保存" }), () => { setId(""); setSecret(""); }); }}>
          <Row name="客户端">
            <div className="st-stack">
              <input className="st-input mono" value={id} onChange={(e) => setId(e.target.value)} autoComplete="off" spellCheck={false} autoCapitalize="off"
                autoCorrect="off" data-1p-ignore placeholder="客户端 ID：…googleusercontent.com" aria-label="客户端 ID" disabled={busy} />
              <Secret set={false} value={secret} onChange={setSecret} hint="客户端密钥：GOCSPX-…" busy={busy} label="客户端密钥" />
              <div className="st-stack-acts">
                <a className="link" href={CLIENTS} target="_blank" rel="noreferrer noopener">去 Google Cloud 拿<span aria-hidden> ↗</span></a>
                <button type="submit" className="btn sm solid" disabled={busy || !both}>保存</button>
              </div>
            </div>
          </Row>
        </form>
      )}
      {pending ? (
        <form onSubmit={(e) => { e.preventDefault(); if (back.trim() && !busy) act(() => gdrive.code(back.trim()), () => ({ text: "已登录" }), () => setBack("")); }}>
          <Row name="登录">
            <div className="st-stack">
              <a className="btn sm" href={g.login!.url} target="_blank" rel="noreferrer noopener">打开授权页<span aria-hidden> ↗</span></a>
              <input className="st-input mono" value={back} onChange={(e) => setBack(e.target.value)} inputMode="url" autoComplete="off" spellCheck={false}
                autoCapitalize="off" autoCorrect="off" data-1p-ignore placeholder="地址栏里 127.0.0.1 开头的整串" aria-label="授权后的地址" disabled={busy} />
              <div className="st-stack-acts">
                <button type="button" className="btn ghost sm" disabled={busy} onClick={() => act(gdrive.cancel, undefined, () => setBack(""))}>取消</button>
                <button type="submit" className="btn sm solid" disabled={busy || !back.trim()}>提交</button>
              </div>
            </div>
          </Row>
        </form>
      ) : (
        <Row name="登录">
          {!g.client ? none : g.state === "auth" ? <><span className="zhu">已失效</span>{off}{login}</>
            : g.token ? <><span>已登录</span>{off}</> : login}
        </Row>
      )}
      {making ? (
        <form onSubmit={(e) => { e.preventDefault(); if (path.trim() && !busy) act(() => gdrive.folder(path.trim()), () => ({ text: "已建好" })); }}>
          <Row name="文件夹">
            <input className="st-input" value={path} onChange={(e) => setPath(e.target.value)} autoComplete="off" spellCheck={false} aria-label="文件夹" disabled={busy} />
            <button type="submit" className="btn sm solid" disabled={busy || !path.trim()}>建好</button>
          </Row>
        </form>
      ) : (
        <Row name="文件夹">
          {g.folder ? <a className="link" href={g.folder.url} target="_blank" rel="noreferrer noopener">{g.folder.path}<span aria-hidden> ↗</span></a> : none}
        </Row>
      )}
      <Row name="作者">
        {rules ? <><Tally c={rules.authors} unit="位" />{rules.authors.on + rules.authors.off + rules.authors.pending > 0 && set("authors")}</> : none}
      </Row>
      <Row name="专题">
        {rules ? <><Tally c={rules.topics} unit="个" />{rules.topics.on + rules.topics.off + rules.topics.pending > 0 && set("topics")}</> : none}
      </Row>
      <Row name="已备份">
        {g.folder && u ? <>{u.videos ? `${u.videos} 期 · ${bytes(u.bytes)}` : "0 期"}{u.drive_free != null ? ` · 网盘剩 ${bytes(u.drive_free)}` : ""}</> : none}
      </Row>
      <Row name="待传">
        {g.folder ? (
          <span>
            {q.due} 期{g.uploading && <> · 正在传 {g.uploading.title || <span className="mono">{g.uploading.id}</span>}</>}
            {q.failed > 0 && <> · <span className="zhu nw">传失败 {q.failed} 期</span></>}
            {q.missed > 0 && <> · <span className="zhu nw">没备份就清理了 {q.missed} 期</span></>}
          </span>
        ) : none}
      </Row>
      <Row name="可删除">
        {dl ? <span>正在移到回收站 {Math.min(dl.done, dl.total)} / {dl.total} 期</span>
          : del ? <>{del.videos ? `${del.videos} 期 · ${bytes(del.bytes)}` : "0 期"}{trashable && (
            <button type="button" className="btn sm" disabled={busy} onClick={() => setSheet("trash")}>删除…</button>
          )}</> : none}
      </Row>
      {g.token && g.folder && (
        <div className="st-acts">
          <button type="button" className="btn sm" disabled={busy}
            onClick={() => act(gdrive.test, (v) => (v.state === "ok" ? { text: "测试通过" } : { text: v.error || "没通过", bad: true }))}>测试</button>
        </div>
      )}
      {sheet === "authors" && <AuthorsSheet onClose={() => setSheet(null)} saved={reload} lost={lost} />}
      {sheet === "topics" && <TopicsSheet onClose={() => setSheet(null)} saved={reload} lost={lost} />}
      {sheet === "trash" && <TrashSheet onClose={() => setSheet(null)} reload={reload} onSent={(v) => { setMine(v); reload(); }} />}
    </>
  );
}
