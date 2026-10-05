import { useId } from "react";

/** 朱文印「审定」— a portrait name-seal, 审 over 定. Pressed once when an issue is approved; still everywhere else. */
export function Yin({ size = 96, fresh = false, title = "已审定" }: { size?: number; fresh?: boolean; title?: string }) {
  const id = useId().replace(/:/g, "");
  const small = size < 48;
  return (
    <span className={`yin-wrap${small ? " small" : ""}`} style={{ width: Math.round(size * 0.66), height: size }}>
      <svg className={`yin${fresh ? " fresh" : ""}`} viewBox="0 0 66 100" role="img" aria-label={title}>
        <defs>
          <filter id={`ink${id}`} x="-6%" y="-6%" width="112%" height="112%">
            {/* low-frequency wobble for the paper's give, sparse pores where the paste did not take */}
            <feTurbulence type="fractalNoise" baseFrequency="0.04" numOctaves="2" seed="7" result="lo" />
            <feDisplacementMap in="SourceGraphic" in2="lo" scale="2.4" result="wob" />
            <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="1" seed="3" result="hi" />
            <feColorMatrix in="hi" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -1.6 1.9" result="pores" />
            <feComposite in="wob" in2="pores" operator="in" />
          </filter>
        </defs>
        <g filter={size < 30 ? undefined : `url(#ink${id})`} fill="var(--zhu)">
          <rect x="3.5" y="3.5" width="59" height="93" rx="5" fill="none" stroke="var(--zhu)" strokeWidth={small ? 6 : 4.5} />
          <text x="33" y="46" textAnchor="middle" fontFamily="AZ Display, Noto Serif SC, serif" fontWeight="900" fontSize="41">审</text>
          <text x="33" y="88" textAnchor="middle" fontFamily="AZ Display, Noto Serif SC, serif" fontWeight="900" fontSize="41">定</text>
        </g>
      </svg>
      {fresh && <i className="yin-bleed" aria-hidden />}
    </span>
  );
}
