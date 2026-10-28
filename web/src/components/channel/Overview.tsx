import { IssueTitle } from "../../cjk";
import { linkProps } from "../../router";
import { big, hrs } from "../Figures";
import { nextScan } from "./bits";
import { activeOf, Avatar, draftOf, modeOf, type Mode } from "./Header";
import type { Channel, ChannelsResp } from "./types";

/* 频道 (/channels): every channel on one page — where it stands, whether intake runs, what it has produced — and the
   way into each one's 看板 or 调试台. Channels waiting for you come first. */

const SPAN = 16;  // weeks drawn in a row (the 看板 shows the same span)

function stage(c: Channel, m: Mode): [string, string] {
  if (m === "discovery") return ["发现中", ""];
  if (m === "tuning") return ["调试中", ""];
  if (c.blocked > 0) return ["交付暂停", "zhu"];
  return [`已定稿 · v${activeOf(c)?.version ?? "?"}`, "ink"];
}

function intake(c: Channel, m: Mode): [string, boolean] {
  if (m !== "locked") return ["", false];
  const ing = c.ingest || {};
  if (c.blocked > 0) return [`${c.blocked} 期等待交付`, true];
  if (!ing.enabled) return ["自动收录已暂停", false];
  const err = ing.scans?.[0]?.error;
  if (err) return ["上次扫描出错", true];
  return [`自动收录${nextScan(ing) ? ` · ${nextScan(ing)}` : ""}`, false];
}

const rank = (c: Channel) => (c.pending_playbook ? 0 : c.blocked > 0 ? 1 : c.stage === "LOCKED" ? 2 : c.playbooks.length ? 3 : 4);

function Weeks({ weeks }: { weeks: number[] }) {
  const w = weeks.slice(-SPAN);
  const top = Math.max(1, ...w);
  return (
    <span className="ch-ovw" role="img" aria-label={`近 ${SPAN} 周 ${w.reduce((a, b) => a + b, 0)} 期`}>
      {w.map((n, i) => <i key={i} className={n ? "" : "z"} style={{ height: `${n ? 18 + (n / top) * 82 : 6}%` }} />)}
    </span>
  );
}

export function Overview({ list }: { list: ChannelsResp }) {
  const chans = [...list.channels].sort((a, b) => rank(a) - rank(b));
  const on = chans.filter((c) => modeOf(c) === "locked" && c.ingest?.enabled).length;
  const waiting = chans.filter((c) => c.pending_playbook).length;
  const status = [`${chans.length} 个频道`, on ? `${on} 个在收录` : "", waiting ? `${waiting} 个待定稿` : ""].filter(Boolean).join(" · ");
  return (
    <div className="chp ch-ovp">
      <header className="pagehead">
        <h1>频道</h1>
        <p className="ph-status">{status}</p>
      </header>
      <ol className="ch-ov">
        {chans.map((c) => {
          const m = modeOf(c);
          const [st, tone] = stage(c, m);
          const [lamp, bad] = intake(c, m);
          const s = c.stats;
          const draft = c.pending_playbook ? draftOf(c) : null;
          return (
            <li key={c.id}>
              <a className="ch-ovr" {...linkProps(`/channels/${encodeURIComponent(c.id)}`)}>
                <Avatar c={c} size={40} />
                <span className="ch-ovn">
                  <span className="ch-ovt">
                    <b>{c.name}</b>
                    <span className={`ch-chip sm ${tone}`}>{st}</span>
                    {draft && <span className="ch-chip sm">待定稿 v{draft.version}</span>}
                  </span>
                  {lamp && <span className={`ch-ovl${bad ? " z" : ""}`}><i className={bad || c.ingest?.enabled ? "on" : ""} />{lamp}</span>}
                  {s.last && (
                    <span className="ch-ovlast">
                      最近 · <IssueTitle title={s.last.headline || s.last.title || s.last.id} />
                    </span>
                  )}
                </span>
                <span className="ch-ovf">
                  <span><b>{big(s.issues)}</b><small>期</small><em>全部</em></span>
                  <span><b>{big(s.month)}</b><small>期</small><em>本月</em></span>
                  <span><b>{s.issues ? (s.units / s.issues).toFixed(0) : "—"}</b><small>单元</small><em>每期</em></span>
                  <span><b>{s.issues ? (s.notes ? (s.notes / s.issues).toFixed(1) : "0") : "—"}</b><small>条</small><em>校注 / 期</em></span>
                  <span className="wide"><b>{hrs(s.seconds)}</b><small>小时</small><em>视频</em></span>
                </span>
                <Weeks weeks={s.weeks} />
              </a>
            </li>
          );
        })}
        <li>
          <a className="ch-ovr q" {...linkProps("/channels/universal")}>
            <span className="ch-ava ch-ava-q" aria-hidden>通</span>
            <span className="ch-ovn"><span className="ch-ovt"><b>通用规程</b>{list.universal && <span className="ch-chip sm">v{list.universal.version}</span>}</span>
              <span className="ch-ovlast">未建档频道使用</span></span>
          </a>
        </li>
      </ol>
    </div>
  );
}
