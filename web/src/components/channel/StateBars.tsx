import { HELD } from "../../util";
import { day, fmt } from "./bits";
import type { Issue } from "./types";

const DELIVERY: Record<string, string> = {
  DELIVERED: "已入 Notion", REGISTERED: "库里已有", BLOCKED: "等待交付", STALE: "等待更新", NOT_STARTED: "等待交付", DELIVERING: "写入中",
  FAILED: "写入失败", HELD: "等跑完",
};

/** 已定稿: the latest issue, the few before it, and how many 校注 each had. */
export function LockedBar({ issues, selected, onPick }: { issues: Issue[]; selected: string | null; onPick: (i: Issue) => void }) {
  if (!issues.length) return <section className="ch-ib empty">定稿后的新视频会出现在这里。</section>;
  const [first, ...rest] = issues;
  const cur = issues.find((i) => i.id === selected) || first;
  const bars = [...issues].reverse();
  const top = Math.max(1, ...issues.map((i) => i.notes));
  const asks = (i: Issue) => i.delivery === "HELD" && !!i.delivery_reason && !!HELD[i.delivery_reason];
  const label = (i: Issue) => (asks(i) ? HELD[i.delivery_reason!] : DELIVERY[i.delivery] || i.delivery);
  const blocked = (i: Issue) => i.delivery === "BLOCKED" || i.delivery === "FAILED" || asks(i);
  const sameYear = (i: Issue) => day(i.published_at).slice(0, 4) === day(first.published_at).slice(0, 4);
  return (
    <section className="ch-ib" aria-label="最近几期">
      <button className={`ch-ib-l${cur.id === first.id ? " on" : ""}`} onClick={() => onPick(first)}>
        <span className="ch-ib-t"><span className="ch-chip sm">最近一期</span><b>{first.title}</b></span>
        <span className="ch-ib-m">
          <span>{day(first.published_at)}</span>{first.duration ? <span>{fmt(first.duration)}</span> : null}
          {first.units != null && <span>{first.units} 单元</span>}<span>校注 {first.notes}</span>
          <span className={blocked(first) ? "z" : ""}>{label(first)}</span>
        </span>
      </button>
      {rest.map((i) => (
        <button key={i.id} className={`ch-ib-o${cur.id === i.id ? " on" : ""}`} onClick={() => onPick(i)}>
          <b>{sameYear(i) ? day(i.published_at).slice(5) : day(i.published_at)}{blocked(i) && <em className="z">{label(i)}</em>}</b><span>{i.title}</span>
        </button>
      ))}
      <span className="ch-ib-tr" title="最近几期的校注数">
        <span>校注</span>
        <span className="ch-bars" aria-hidden>{bars.map((i) => <i key={i.id} className={i.id === first.id ? "last" : ""} style={{ height: 6 + Math.round((i.notes / top) * 16) }} />)}</span>
      </span>
    </section>
  );
}

/** 发现中: the one issue there is, made with the universal playbook. */
export function DiscoveryBar({ issue }: { issue: Issue | null }) {
  if (!issue) return <section className="ch-ib empty">这个频道还没有任何一期。在页头粘贴一个视频链接付印第一期。</section>;
  return (
    <section className="ch-ib" aria-label="样片">
      <span className="ch-ib-l static">
        <span className="ch-ib-t"><span className="ch-thumb"><em>{fmt(issue.duration)}</em></span><b>{issue.title}</b></span>
        <span className="ch-ib-m"><span>通用规程</span>{issue.units != null && <span>{issue.units} 单元</span>}<span>校注 {issue.notes}</span></span>
      </span>
    </section>
  );
}
