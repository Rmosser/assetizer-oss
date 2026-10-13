import { createContext, useContext } from "react";

/** Small, dense building blocks for the 频道 configuration tool. */

export type Tone = "ok" | "warn" | "bad" | "muted" | "ink";

export function Chip({ tone = "muted", children, title }: { tone?: Tone; children: React.ReactNode; title?: string }) {
  return <span className={`chip ${tone}`} title={title}>{children}</span>;
}

export const STAGE: Record<string, { label: string; tone: Tone }> = {
  TUNING: { label: "调试中", tone: "warn" },
  LOCKED: { label: "已定稿", tone: "ok" },
};
export const PB: Record<string, { label: string; tone: Tone }> = {
  ACTIVE: { label: "生效中", tone: "ok" },
  NEEDS_CONFIRMATION: { label: "草稿", tone: "warn" },
  SUPERSEDED: { label: "已替代", tone: "muted" },
  FAILED: { label: "编译失败", tone: "bad" },
};

export function Field({ label, hint, children, wide }: { label: string; hint?: React.ReactNode; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className={`cfg-field${wide ? " wide" : ""}`}>
      <span className="cfg-field-l">{label}</span>
      <div className="cfg-field-c">
        {children}
        {hint && <p className="cfg-hint">{hint}</p>}
      </div>
    </div>
  );
}

export function Seg<T extends string>({ value, options, onChange, label }: {
  value: T; options: { value: T; label: React.ReactNode }[]; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} role="radio" aria-checked={o.value === value} className={o.value === value ? "on" : ""} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Toggle({ on, onChange, children, disabled }: { on: boolean; onChange: (v: boolean) => void; children?: React.ReactNode; disabled?: boolean }) {
  return (
    <button role="switch" aria-checked={on} disabled={disabled} className={`switch${on ? " on" : ""}`} onClick={() => onChange(!on)}>
      <i aria-hidden />{children}
    </button>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="cfg-empty">{children}</div>;
}

export function Note({ tone = "muted", children }: { tone?: Tone; children: React.ReactNode }) {
  return <p className={`cfg-note ${tone}`} role={tone === "bad" ? "alert" : undefined}>{children}</p>;
}

/** Panels report unsaved edits so the page can ask before they are lost. */
export const DirtyCtx = createContext<(key: string, dirty: boolean) => void>(() => {});
export const useDirty = () => useContext(DirtyCtx);

export const day = (s?: string | null) => (s && /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : (s || "").slice(0, 10));
export const mins = (d?: number | null) => (d ? `${Math.round(d / 60)} 分钟` : "—");
export const stamp = (iso?: string | null) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(+d)) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
