import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

/** Bottom sheet (mobile) built on <dialog>: focus trap, Esc and backdrop close for free. */
export function Sheet({ title, onClose, tall = false, children, labelledBy }: {
  title?: React.ReactNode; onClose: () => void; tall?: boolean; children: React.ReactNode; labelledBy?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (!d.open) d.showModal();
    const onCancel = (e: Event) => { e.preventDefault(); onClose(); };
    d.addEventListener("cancel", onCancel);
    document.documentElement.classList.add("modal-open");
    return () => { d.removeEventListener("cancel", onCancel); document.documentElement.classList.remove("modal-open"); };
  }, [onClose]);
  return createPortal(
    <dialog ref={ref} className={`sheet${tall ? " tall" : ""}`} aria-labelledby={labelledBy}
      onClick={(e) => { if (e.target === ref.current) onClose(); }}>
      <div className="sheet-body">
        <div className="sheet-grip" aria-hidden />
        {title && <h3 className="sheet-title">{title}</h3>}
        {children}
      </div>
    </dialog>,
    document.body,
  );
}
