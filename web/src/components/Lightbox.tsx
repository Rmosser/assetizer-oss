import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { media } from "../api";
import { typeset } from "../cjk";
import { tc } from "../util";

type Shot = { id: number; start: number; meta?: { reason?: string } };

/** Screenshot viewer: <dialog>, swipe/arrow between the unit's frames, tap to zoom on phones. */
export function Lightbox({ shots, index, onClose }: { shots: Shot[]; index: number; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const [i, setI] = useState(index);
  const [zoom, setZoom] = useState(false);
  useEffect(() => {
    const d = ref.current!;
    d.showModal();
    const onCancel = (e: Event) => { e.preventDefault(); onClose(); };
    d.addEventListener("cancel", onCancel);
    document.documentElement.classList.add("modal-open");
    requestAnimationFrame(() => track.current?.children[index]?.scrollIntoView({ inline: "start", block: "nearest" }));
    return () => { d.removeEventListener("cancel", onCancel); document.documentElement.classList.remove("modal-open"); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const go = (n: number) => {
    const k = Math.max(0, Math.min(shots.length - 1, n));
    setZoom(false);
    track.current?.children[k]?.scrollIntoView({ inline: "start", block: "nearest", behavior: "smooth" });
    setI(k);
  };
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key === "ArrowRight") go(i + 1);
      if (e.key === "ArrowLeft") go(i - 1);
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  });
  const onScroll = () => {
    const el = track.current;
    if (el) setI(Math.round(el.scrollLeft / el.clientWidth));
  };
  const s = shots[i];
  return createPortal(
    <dialog ref={ref} className="lightbox" aria-label="截图">
      <div className="lb-track" ref={track} onScroll={onScroll}>
        {shots.map((x, k) => (
          <figure key={x.id} className={`lb-frame${zoom && k === i ? " zoom" : ""}`}>
            <img src={media(x.id)} alt={x.meta?.reason || ""} onDoubleClick={() => setZoom(!zoom)} onClick={() => setZoom(!zoom)} />
          </figure>
        ))}
      </div>
      <div className="lb-bar">
        <p><span className="num">{tc(s?.start)}</span>{s?.meta?.reason && typeset(s.meta.reason)}</p>
        {shots.length > 1 && (
          <div className="lb-nav">
            <button onClick={() => go(i - 1)} disabled={i === 0} aria-label="上一张">‹</button>
            <span className="num">{i + 1} / {shots.length}</span>
            <button onClick={() => go(i + 1)} disabled={i === shots.length - 1} aria-label="下一张">›</button>
          </div>
        )}
      </div>
      <button className="lb-close" onClick={onClose} aria-label="关闭">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg>
      </button>
    </dialog>,
    document.body,
  );
}
