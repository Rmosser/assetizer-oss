import { useState } from "react";

/** A command to run in a terminal, with a copy button. */
export function Cmd({ text }: { text: string }) {
  const [ok, setOk] = useState(false);
  return (
    <span className="cmd">
      <code>{text}</code>
      <button type="button" onClick={async () => { try { await navigator.clipboard.writeText(text); setOk(true); setTimeout(() => setOk(false), 1500); } catch { /* ignore */ } }}>
        {ok ? "已复制" : "复制"}
      </button>
    </span>
  );
}
