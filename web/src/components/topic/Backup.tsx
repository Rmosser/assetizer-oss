/* 网盘备份 on a topic's head: only what went wrong — the drive's sign-in gone bad (with the way to sign in again, whether or
   not this topic has a failed video yet), videos whose backup failed (trying again), and those cleared from the disk before
   they were backed up. A backup that went well says nothing. */
import { useState } from "react";
import type { TopicBackup } from "../../api";
import { linkProps } from "../../router";
import "../../topic.css";

const clip = (s: string, n = 80) => (s.length > n ? `${s.slice(0, n)}…` : s);

export function BackupTrouble({ b, onRetry }: { b: TopicBackup | undefined; onRetry: () => Promise<unknown> }) {
  const [busy, setBusy] = useState(false);
  if (!b || (!b.auth && !(b.failed > 0) && !(b.missed > 0))) return null;
  const retry = async () => {
    setBusy(true);
    try { await onRetry(); } finally { setBusy(false); }
  };
  return (
    <>
      {b.auth ? (
        <p className="tp-ntrouble bad" role="alert">
          <span>{b.failed > 0 ? `${b.failed} 期备份失败：网盘登录失效` : "网盘登录失效"}</span>
          <a className="tp-nact" {...linkProps("/settings#gdrive")}>去重新登录</a>
        </p>
      ) : b.failed > 0 && (
        <p className="tp-ntrouble bad" role="alert">
          <span title={b.error || undefined}>{b.failed} 期备份失败{b.error ? `：${clip(b.error)}` : ""}</span>
          <button className="tp-nact" disabled={busy} onClick={retry}>重试</button>
        </p>
      )}
      {b.missed > 0 && <p className="tp-ntrouble bad"><span>{b.missed} 期没备份就清理了</span></p>}
    </>
  );
}
