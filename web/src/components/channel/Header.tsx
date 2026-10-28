import { useEffect, useRef, useState } from "react";
import { post } from "../../api";
import { navigate } from "../../router";
import { Icon, nextScan } from "./bits";
import type { Channel, ChannelsResp } from "./types";

export type Mode = "discovery" | "tuning" | "locked";
export const modeOf = (c: Channel): Mode => (!c.playbooks.length ? "discovery" : c.stage === "LOCKED" ? "locked" : "tuning");
export const activeOf = (c: Channel) => c.playbooks.find((p) => p.status === "ACTIVE");
export const draftOf = (c: Channel) => c.playbooks.find((p) => p.status === "NEEDS_CONFIRMATION");

function status(c: Channel): { text: string; tone: "" | "ink" | "zhu" } {
  const m = modeOf(c);
  if (m === "discovery") return { text: "发现中", tone: "" };
  if (m === "tuning") return { text: "调试中", tone: "" };
  if (c.blocked > 0) return { text: "交付暂停", tone: "zhu" };
  return { text: `已定稿 · v${activeOf(c)?.version ?? "?"}`, tone: "ink" };
}

const ALERT_TEXT: Record<string, string> = { "notion-auth": "Notion 未连接", YOUTUBE_AUTH: "YouTube 需要登录", AGY_AUTH_REQUIRED: "模型需要登录", DISK_LOW: "磁盘空间不足" };

export function Avatar({ c, size = 38 }: { c: { name: string }; size?: number }) {
  return <span className="ch-ava" style={{ width: size, height: size, fontSize: size * 0.42 }} aria-hidden>{c.name.replace(/^[@＠]/, "").slice(0, 1)}</span>;
}

/** The page title as a menu of every channel and the universal playbook. */
export function Switcher({ list, current, children }: { list: ChannelsResp; current: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const pop = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!pop.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    addEventListener("mousedown", close);
    addEventListener("keydown", esc);
    return () => { removeEventListener("mousedown", close); removeEventListener("keydown", esc); };
  }, [open]);
  const go = (id: string) => { setOpen(false); navigate(`/channels/${encodeURIComponent(id)}`, { quiet: true }); };
  return (
    <div className="ch-sw" ref={pop}>
      <button className="ch-swbtn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>
        {children}<Icon name="chevd" size={16} />
      </button>
      {open && (
        <div className="ch-swpop" role="menu">
          <button role="menuitem" className="ch-swi q" onClick={() => { setOpen(false); navigate("/channels", { quiet: true }); }}>
            <span className="nm">全部频道</span>
          </button>
          <div className="ch-swsep" />
          {list.channels.map((c) => {
            const s = status(c);
            return (
              <button key={c.id} role="menuitem" className={`ch-swi${c.id === current ? " on" : ""}`} onClick={() => go(c.id)}>
                <Avatar c={c} size={26} /><span className="nm">{c.name}</span><span className={`sst${s.tone === "zhu" ? " z" : ""}`}>{s.text.replace(/ · v\d+$/, "")}</span>
              </button>
            );
          })}
          <div className="ch-swsep" />
          <button role="menuitem" className={`ch-swi q${current === "universal" ? " on" : ""}`} onClick={() => go("universal")}>
            <span className="nm">通用规程</span><span className="sst">未建档频道使用</span>
          </button>
        </div>
      )}
    </div>
  );
}

export function Header({ ch, list, alerts, testCopy, onLock, onRetune, lockVersion, reload, tabs }: {
  ch: Channel; list: ChannelsResp; alerts: string[]; testCopy: boolean; lockVersion: number | null;
  onLock: () => void; onRetune: () => Promise<void>; reload: () => void; tabs?: React.ReactNode;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(ch.name);
  const [confirmRetune, setConfirmRetune] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const mode = modeOf(ch);
  const st = status(ch);
  const rename = async () => {
    setRenaming(false);
    if (!name.trim() || name.trim() === ch.name) return setName(ch.name);
    try { await post(`/api/channels/${ch.id}`, { name: name.trim() }); reload(); } catch (e) { setErr((e as Error).message); setName(ch.name); }
  };
  const ing = ch.ingest || {};
  const lamp = ch.blocked > 0 ? { text: `${ch.blocked} 期等待交付`, zhu: true }
    : ing.enabled && ing.scans?.[0]?.members ? { text: "会员视频收不全", zhu: true }
    : ing.enabled ?{ text: `自动收录中${nextScan(ing) ? ` · ${nextScan(ing)}` : ""}`, zhu: false }
    : { text: "自动收录已暂停", zhu: false };
  return (
    <header className="ch-head">
      {renaming ? (
        <div className="ch-sw">
          <span className="ch-rename">
            <Avatar c={ch} />
            <input autoFocus value={name} onChange={(e) => setName(e.target.value)} onBlur={rename}
              onKeyDown={(e) => { if (e.key === "Enter") rename(); if (e.key === "Escape") { setName(ch.name); setRenaming(false); } }} aria-label="频道名称" />
          </span>
        </div>
      ) : (
        <Switcher list={list} current={ch.id}><Avatar c={ch} /><h1>{ch.name}</h1></Switcher>
      )}
      <span className={`ch-chip ${st.tone}`}>{st.text}</span>
      {!renaming && <button className="ch-icb ch-ren" aria-label="改频道名" onClick={() => setRenaming(true)}><Icon name="pen" size={14} /></button>}
      {tabs}
      <div className="ch-acts">
        {(alerts.length > 0 || testCopy) && (
          <span className="ch-lamps">
            {alerts.map((a) => <span key={a} className="ch-lamp z"><i />{ALERT_TEXT[a] || a}</span>)}
            {testCopy && <span className="ch-tag">测试副本</span>}
          </span>
        )}
        {mode === "locked" && (
          <>
            <span className={`ch-lampline${lamp.zhu ? " z" : ""}`}><i className={ing.enabled || lamp.zhu ? "on" : ""} />{lamp.text}</span>
            {confirmRetune ? (
              <span className="ch-confirm">
                <span>回到调试期会暂停自动收录</span>
                <button className="ch-btn sm" onClick={() => setConfirmRetune(false)}>取消</button>
                <button className="ch-btn sm solid" onClick={async () => { setConfirmRetune(false); try { await onRetune(); } catch (e) { setErr((e as Error).message); } }}>回到调试期</button>
              </span>
            ) : <button className="ch-btn sm" onClick={() => setConfirmRetune(true)}>回到调试期</button>}
          </>
        )}
        {mode === "tuning" && lockVersion != null && (
          <button className="ch-btn solid" onClick={onLock}><Icon name="lock" size={14} /><span>定稿 v{lockVersion}</span></button>
        )}
      </div>
      {err && <p className="ch-err" role="alert">{err}</p>}
    </header>
  );
}
