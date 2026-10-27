import { useCallback, useEffect, useId, useImperativeHandle, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { reduceMotion } from "../util";

export type SheetHandle = { close: (then?: () => void) => void };

/** Bottom sheet (phone) / dialog (desktop) built on <dialog>: focus trap, Esc and backdrop close for free.
 *  On phones the grip really drags: pull down past 80px to close, less and it settles back.
 *  Every close plays the sheet out first; content that closes it itself calls `ref.close(then)`,
 *  where `then` (default onClose) is what unmounts the sheet. */
export function Sheet({ title, onClose, tall = false, children, ref: handle }: {
  title?: React.ReactNode; onClose: () => void; tall?: boolean; children: React.ReactNode; ref?: React.Ref<SheetHandle>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const drag = useRef<{ y: number; dy: number } | null>(null);
  const after = useRef<(() => void) | null>(null);
  const [leaving, setLeaving] = useState(false);
  const id = useId();
  const close = useCallback((then?: () => void) => {
    if (after.current) return;
    const done = then || onClose;
    if (reduceMotion()) { done(); return; }
    after.current = done;
    setLeaving(true);
    // the dialog is closed before handing over, so a `then` that keeps the sheet mounted cannot leave an invisible modal behind
    window.setTimeout(() => { ref.current?.close(); done(); }, 280);
  }, [onClose]);
  useImperativeHandle(handle, () => ({ close }), [close]);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (!d.open) d.showModal();
    const onCancel = (e: Event) => { e.preventDefault(); close(); };
    d.addEventListener("cancel", onCancel);
    document.documentElement.classList.add("modal-open");
    return () => { d.removeEventListener("cancel", onCancel); document.documentElement.classList.remove("modal-open"); };
  }, [close]);
  const move = (dy: number, settle = false) => {
    const d = ref.current;
    if (!d) return;
    d.style.transition = settle ? "transform 0.25s var(--ease)" : "none";
    d.style.transform = dy > 0 ? `translateY(${dy}px)` : "";
  };
  return createPortal(
    <dialog ref={ref} className={`sheet${tall ? " tall" : ""}${leaving ? " leaving" : ""}`} aria-labelledby={title ? id : undefined}
      onClick={(e) => { if (e.target === ref.current) close(); }}>
      <div className="sheet-body">
        <div className="sheet-grip" aria-hidden
          onPointerDown={(e) => { (e.target as HTMLElement).setPointerCapture(e.pointerId); drag.current = { y: e.clientY, dy: 0 }; }}
          onPointerMove={(e) => { if (drag.current) { drag.current.dy = e.clientY - drag.current.y; move(drag.current.dy); } }}
          onPointerUp={() => { const dy = drag.current?.dy || 0; drag.current = null; if (dy > 80) close(); else move(0, true); }}
          onPointerCancel={() => { drag.current = null; move(0, true); }} />
        {title && <h3 className="sheet-title" id={id}>{title}</h3>}
        {children}
      </div>
    </dialog>,
    document.body,
  );
}
