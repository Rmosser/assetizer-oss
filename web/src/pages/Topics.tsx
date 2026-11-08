/* 专题: one question and a batch of videos, each read for it, then put together into one answer (design §R8). The list:
   each topic's question, its answer's first sentence or where its reads stand, and the batch at a glance. */
import { Fragment, useState } from "react";
import { Words } from "../cjk";
import { Deck, heldText, hours, Mix, TOPIC_WAIT, when } from "../components/topic/bits";
import { Picker } from "../components/topic/Picker";
import { Empty, Fault, Stale, Unroll } from "../components/States";
import { usePoll } from "../hooks";
import { linkProps, useRestoreScroll } from "../router";
import "../topic.css";

type Row = {
  id: number; question: string; paused: boolean; created_at: string; updated_at: string;
  videos: number; excluded: number; queued: number; running: number; waiting: number; done: number; failed: number;
  high: number; some: number; none: number; hours: number; answer: string | null; synth_state: string | null; thumbs: string[];
};

/** Where a topic stands, in one line: its answer once there is one, else how far its reads are. `held`: the reads
 *  that have not started wait for a quota (the page head says which) */
function standing(t: Row, held: boolean): { text: string; tone?: "bad" | "quiet" } {
  const open = t.running + t.queued + t.waiting;
  if (t.paused) return { text: `已暂停 · 读完 ${t.done}/${t.videos}`, tone: "quiet" };
  if (open) {
    const where = [t.running ? `在读 ${t.running} 期` : "",
      (t.queued || t.waiting) && held ? TOPIC_WAIT.TOPIC_QUOTA : t.running ? "" : t.waiting ? `${t.waiting} 期在等` : "排队"];
    return { text: [...where, `读完 ${t.done}/${t.videos}`].filter(Boolean).join(" · "), tone: "quiet" };
  }
  if (t.synth_state === "QUEUED" || t.synth_state === "RUNNING") return { text: "正在综合", tone: "quiet" };
  if (t.answer) return { text: t.answer };
  if (t.failed && !t.done) return { text: `${t.failed} 期没读成`, tone: "bad" };
  return { text: t.synth_state === "FAILED" ? "综合没成" : "读完了", tone: t.synth_state === "FAILED" ? "bad" : "quiet" };
}

function TopicRow({ t, held }: { t: Row; held: boolean }) {
  const s = standing(t, held);
  const figs = [`${t.videos} 期`, t.hours ? hours(t.hours) : null, t.high ? `${t.high} 期值得看` : null,
    t.failed ? <span className="tp-bad">{t.failed} 期没读成</span> : null].filter(Boolean);
  return (
    <li>
      <a className="tp-row" {...linkProps(`/topics/${t.id}`)}>
        <Deck thumbs={t.thumbs} />
        <div className="tp-row-t">
          <h2><Words>{t.question}</Words></h2>
          <p className={`tp-row-s${s.tone ? ` ${s.tone}` : ""}`}>{s.text}</p>
          <p className="tp-row-m">
            <Mix c={t} />
            <span>{figs.map((f, i) => <Fragment key={i}>{i > 0 && " · "}{f}</Fragment>)}</span>
          </p>
        </div>
        <time className="tp-row-d" dateTime={t.updated_at}>{when(t.updated_at)}</time>
      </a>
    </li>
  );
}

export default function Topics() {
  const { data, error, loadedAt, reload } = usePoll<any>("/api/topics", 8000);
  const [making, setMaking] = useState(false);
  useRestoreScroll(!!data);
  if (!data) return <main className="page tp-list">{error ? <Fault error={error} retry={reload} /> : <Unroll />}</main>;
  const topics: Row[] = data.topics;
  const reading = topics.reduce((n, t) => n + (t.paused ? 0 : t.running), 0);
  const held = heldText(data.held);
  const make = <button className="btn solid" onClick={() => setMaking(true)}>新专题</button>;
  return (
    <main className="page tp-list">
      <Stale error={error} loadedAt={loadedAt} />
      <header className="pagehead">
        <h1>专题</h1>
        {topics.length > 0 && (
          <div className="tp-ph">
            <p className="ph-status">
              <i className={`lamp ${reading ? "ok" : "idle"}`} aria-hidden />
              {[reading ? `在读 ${reading} 期` : "", held].filter(Boolean).join(" · ") || `${topics.length} 个专题`}
            </p>
            {make}
          </div>
        )}
      </header>
      {topics.length ? (
        <ol className="tp-rows">{topics.map((t) => <TopicRow key={t.id} t={t} held={!!held} />)}</ol>
      ) : (
        <Empty title="还没有专题" action={make}>一个问题，一批视频：逐期读完，再合成一个回答。</Empty>
      )}
      {making && <Picker budget={data.budget} onClose={() => { setMaking(false); reload(); }} />}
    </main>
  );
}
