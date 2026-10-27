import { media } from "../../api";
import { cnDate, fmt, Icon, Track } from "./bits";
import type { Compare, SourceDetail } from "./types";

const TAG: Record<string, string> = { split: "拆出", merge: "合并", new: "新" };

export type FieldKeys = { url: string; title: string; channel: string; date: string; access: string };

/** 原片: the YouTube page the issue comes from, with the run's knowledge units on its time line. */
export function SourcePane({ detail, loading, channelName, track, flags, version, cmp, prevVersion, secOf, badge, keys, onJump, switcher }: {
  detail: SourceDetail | null; loading: boolean; channelName: string; track: [number, number, number][]; flags: number[]; version: string;
  cmp: Compare | null; prevVersion: number | null; secOf: Map<string, number>; badge: (i: number) => string | null;
  keys: FieldKeys; onJump: (sec: number) => void; switcher?: React.ReactNode;
}) {
  const label = switcher || <span className="pt">原片</span>;
  if (!detail) return <div className="ch-pane ch-yt"><div className="ch-ph">{label}</div><div className="ch-pw empty" aria-busy={loading}>{!loading && "选一格样片看结果"}</div></div>;
  const s = detail.source;
  const info = s.info || {};
  const live = ["was_live", "post_live", "is_live"].includes(info.live_status) || !!info.was_live;
  const member = ["subscriber_only", "premium_only"].includes(info.availability);
  const dur = s.duration || (track.length ? track[track.length - 1][1] : 0);
  const units = detail.units;
  const poster = detail.assets.find((a) => a.kind === "poster" && a.status === "GENERATED") || detail.assets.find((a) => a.kind === "screenshot");
  const url = s.url || `https://www.youtube.com/watch?v=${s.id}`;
  const uKey = (id: string) => `u:${id}${secOf.has(id) ? ` c:${secOf.get(id)}` : ""}`;
  const jump = (id: string) => { const i = secOf.get(id); if (i !== undefined) onJump(i); };
  const rows = cmp
    ? [...cmp.b_units.map((u) => ({ u, old: false, tag: TAG[u.change] || "" })),
       ...cmp.a_units.filter((u) => u.change === "gone").map((u) => ({ u, old: true, tag: `v${prevVersion} · 不再单列` }))]
      .sort((x, y) => x.u.start - y.u.start || Number(x.old) - Number(y.old))
    : units.map((u) => ({ u, old: false, tag: "" }));
  return (
    <div className="ch-pane ch-yt">
      <div className="ch-ph">{label}<span className="grow" /><a className="ch-phl" href={url} target="_blank" rel="noreferrer">在 YouTube 打开 ↗</a></div>
      <div className={`ch-pw yt${loading ? " loading" : ""}`} aria-busy={loading}>
        <div className="ytbar lt-ring" data-k={keys.url} data-side="src"><Icon name="lock" size={12} />{url.replace(/^https?:\/\/(www\.)?/, "")}</div>
        <a className="player" href={url} target="_blank" rel="noreferrer" aria-label="在 YouTube 播放">
          {poster ? <img src={media(poster.id, 640)} alt="" loading="lazy" /> : <span className="titlecard"><span>{s.title}</span></span>}
          {(live || member) && <span className="live lt-ring" data-k={keys.access} data-side="src">{member ? "会员专享" : "直播回放"}</span>}
          <span className="play"><Icon name="play" size={18} fill /></span>
          <span className="dur">{fmt(dur)}</span>
        </a>
        <div className="ut">
          {cmp ? (
            <>
              <div className="ur"><span className="lb">v{prevVersion}</span>
                <Track units={cmp.a_units.map((u) => [u.start, u.end, u.learning_priority || 2])} flags={[]} dur={dur}
                  keyOf={(i) => `a:${cmp.a_units[i].id}`} mark={(i) => (cmp.a_units[i].change === "gone" ? "gn" : "")} tall /></div>
              <div className="ur"><span className="lb">{version}</span>
                <Track units={cmp.b_units.map((u) => [u.start, u.end, u.learning_priority || 2])} flags={flags} dur={dur}
                  keyOf={(i) => uKey(cmp.b_units[i].id)} mark={(i) => ({ split: "sp", new: "nw" } as Record<string, string>)[cmp.b_units[i].change] || ""}
                  onPick={(i) => jump(cmp.b_units[i].id)} tall /></div>
            </>
          ) : (
            <div className="ur"><span className="lb">{version}</span>
              <Track units={track} flags={flags} dur={dur} keyOf={(i) => (units[i] ? uKey(units[i].id) : "")} onPick={(i) => units[i] && jump(units[i].id)} tall /></div>
          )}
        </div>
        <h2 className="ytt"><span className="lt-ring" data-k={keys.title} data-side="src">{s.title}</span></h2>
        <div className="ytc">
          <span className="a" aria-hidden>{channelName.replace(/^[@＠]/, "").slice(0, 1)}</span>
          <b className="lt-ring" data-k={keys.channel} data-side="src">{s.channel_title || channelName}</b>
          <span className="sub">订阅</span>
        </div>
        <div className="ytd"><b>{live && <span>直播时间：</span>}<span className="lt-ring" data-k={keys.date} data-side="src">{cnDate(s.published_at)}</span></b></div>
        <div className="ytl">
          <div className="hd">{cmp ? `知识单元 · v${prevVersion} → ${version}` : `知识单元 · ${version}`}</div>
          {rows.map(({ u, old, tag }) => {
            const i = secOf.get(u.id);
            const b = !cmp && i !== undefined ? badge(i) : null;
            return (
              <button key={(old ? "a" : "u") + u.id} className={`li lt-bg${old ? " gone" : ""}`} data-k={old ? `a:${u.id}` : uKey(u.id)} data-side="src"
                onClick={() => !old && jump(u.id)}>
                <em>{fmt(u.start)}</em><span className="lt">{u.title}</span>
                {tag && <b className="utag">{tag}</b>}
                {b && <b className="cn2">{b}</b>}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
