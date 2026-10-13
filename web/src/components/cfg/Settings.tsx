import { useEffect, useState } from "react";
import { post } from "../../api";
import { Field, Note, Toggle, stamp, useDirty } from "./ui";

/** 设置: what the channel is, and how its new videos come in. */
export default function SettingsPanel({ c, reload }: { c: any; reload: () => void }) {
  const [name, setName] = useState(c.name || "");
  const [why, setWhy] = useState(c.why || "");
  const [minM, setMinM] = useState<number>(c.ingest?.min_minutes || 10);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const setDirty = useDirty();
  const infoDirty = name !== (c.name || "") || why !== (c.why || "");
  const ing = c.ingest || {};
  const minDirty = minM !== (ing.min_minutes || 10);
  useEffect(() => { setDirty("settings", infoDirty || minDirty); return () => setDirty("settings", false); }, [infoDirty, minDirty, setDirty]);
  useEffect(() => { setName(c.name || ""); setWhy(c.why || ""); }, [c.name, c.why]);
  useEffect(() => { setMinM(c.ingest?.min_minutes || 10); }, [c.ingest?.min_minutes]);
  const tracked = c.mode === "TRACKED";
  const canIngest = tracked && !!c.youtube_channel_id && c.stage === "LOCKED";

  const run = async (fn: () => Promise<any>, ok: string) => {
    setBusy(true); setMsg(null);
    try { await fn(); setMsg({ tone: "ok", text: ok }); reload(); } catch (e) { setMsg({ tone: "bad", text: (e as Error).message }); }
    setBusy(false);
  };
  const saveInfo = () => run(() => post(`/api/channels/${c.id}`, { name: name.trim(), why: why.trim() }), "已保存频道信息。");
  const setIngest = (enabled: boolean) => {
    if (enabled && !confirm(`开启自动收录：「${c.name}」之后发布、${minM} 分钟以上的视频会自动生产，并写入 Notion。确定？`)) return;
    run(() => post(`/api/channels/${c.id}/ingest`, { enabled, min_minutes: minM }), enabled ? "已开启自动收录。" : "已关闭自动收录。");
  };
  const saveMin = () => run(() => post(`/api/channels/${c.id}/ingest`, { enabled: !!ing.enabled, min_minutes: minM }), "已更新最短时长。");

  return (
    <div className="set">
      <section className="cfg-sec">
        <h3 className="cfg-sec-h">基本信息</h3>
        <div className="cfg-form">
          <Field label="频道名称"><input className="cfg-input" value={name} onChange={(e) => setName(e.target.value)} aria-label="频道名称" /></Field>
          <Field label="为什么看" hint="写给自己：这个频道值得长期看的原因。学习目标（编译成规程的那份）在「学习目标」里改。">
            <textarea className="cfg-input area" value={why} onChange={(e) => setWhy(e.target.value)} rows={3} aria-label="为什么看这个频道" />
          </Field>
          <Field label="YouTube 频道">
            {c.youtube_channel_id ? (
              <span className="mono">{c.youtube_channel_id} <a className="muted" href={`https://www.youtube.com/channel/${c.youtube_channel_id}`} target="_blank" rel="noreferrer">打开 ↗</a></span>
            ) : <span className="muted">未关联</span>}
          </Field>
          <Field label="频道 ID"><span className="mono muted">{c.id}</span></Field>
          <Field label="模式"><span>{tracked ? "长期关注" : "发现中"}</span></Field>
        </div>
        <div className="cfg-toolbar">
          <button className="btn solid sm" disabled={busy || !infoDirty || !name.trim()} onClick={saveInfo}>保存</button>
          <button className="btn sm ghost" disabled={!infoDirty} onClick={() => { setName(c.name || ""); setWhy(c.why || ""); }}>还原</button>
        </div>
      </section>

      {tracked && (
        <section className="cfg-sec">
          <h3 className="cfg-sec-h">自动收录</h3>
          <div className="cfg-form">
            <Field label="状态" hint={!c.youtube_channel_id ? "没有关联 YouTube 频道，无法自动收录。"
              : c.stage !== "LOCKED" ? "调试中的频道不自动收录；定稿时会自动开启。" : "只收录开启之后发布的视频，不回溯旧视频。每 30 分钟检查一次。"}>
              <Toggle on={!!ing.enabled} disabled={busy || (!canIngest && !ing.enabled)} onChange={setIngest}>{ing.enabled ? "已开启" : "已关闭"}</Toggle>
            </Field>
            <Field label="最短时长" hint="短于这个时长的视频（短视频、片段）不收录。">
              <span className="cfg-inline">
                <input className="cfg-input num w-s" type="number" min={0} max={240} value={minM} onChange={(e) => setMinM(Math.max(0, Number(e.target.value) || 0))} aria-label="最短时长（分钟）" />
                <span className="muted">分钟</span>
                <button className="btn xs" disabled={busy || !minDirty} onClick={saveMin}>保存</button>
              </span>
            </Field>
            <Field label="收录起点"><span className="num">{ing.since ? stamp(ing.since) : "—"}</span></Field>
            <Field label="上次检查"><span className="num">{ing.last_poll ? stamp(ing.last_poll) : "—"}</span></Field>
            <Field label="来源" hint="YouTube 的 RSS 目前对所有频道都返回错误，系统会改用 yt-dlp 读取频道的「视频」和「直播」列表（发布日期按天）。">
              <span>RSS，失败时用 yt-dlp 频道列表</span>
            </Field>
          </div>
        </section>
      )}
      {msg && <Note tone={msg.tone}>{msg.text}</Note>}
    </div>
  );
}
