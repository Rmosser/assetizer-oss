import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

/** Bottom sheet (phone) / dialog (desktop) built on <dialog>: focus trap, Esc and backdrop close for free.
 *  On phones the grip really drags: pull down past 80px to close. */
export function Sheet({ title, onClose, tall = false, children }: {
  title?: React.ReactNode; onClose: () => void; tall?: boolean; children: React.ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const drag = useRef<{ y: number; dy: number } | null>(null);
  const id = useId();
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (!d.open) d.showModal();
    const onCancel = (e: Event) => { e.preventDefault(); onClose(); };
    d.addEventListener("cancel", onCancel);
    document.documentElement.classList.add("modal-open");
    return () => { d.removeEventListener("cancel", onCancel); document.documentElement.classList.remove("modal-open"); };
  }, [onClose]);
  const move = (dy: number) => { if (ref.current) ref.current.style.transform = dy > 0 ? `translateY(${dy}px)` : ""; };
  return createPortal(
    <dialog ref={ref} className={`sheet${tall ? " tall" : ""}`} aria-labelledby={title ? id : undefined}
      onClick={(e) => { if (e.target === ref.current) onClose(); }}>
      <div className="sheet-body">
        <div className="sheet-grip" aria-hidden
          onPointerDown={(e) => { (e.target as HTMLElement).setPointerCapture(e.pointerId); drag.current = { y: e.clientY, dy: 0 }; }}
          onPointerMove={(e) => { if (drag.current) { drag.current.dy = e.clientY - drag.current.y; move(drag.current.dy); } }}
          onPointerUp={() => { const dy = drag.current?.dy || 0; drag.current = null; if (dy > 80) onClose(); else move(0); }}
          onPointerCancel={() => { drag.current = null; move(0); }} />
        {title && <h3 className="sheet-title" id={id}>{title}</h3>}
        {children}
      </div>
    </dialog>,
    document.body,
  );
}
