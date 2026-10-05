import { useEffect, useState } from "react";
import type { PollError } from "../hooks";
import { ago } from "../util";

/** 展卷 — a hairline unrolls from the centre, then the word appears. */
export function Unroll() {
  return (
    <div className="unroll" role="status" aria-label="正在载入">
      <i />
      <span>展卷</span>
    </div>
  );
}

/** Nothing on screen yet and the request failed. */
export function Fault({ error, retry }: { error: PollError; retry: () => void }) {
  const [left, setLeft] = useState(15);
  useEffect(() => {
    setLeft(15);
    const t = setInterval(() => setLeft((s) => (s <= 1 ? 15 : s - 1)), 1000);
    return () => clearInterval(t);
  }, [error.at]);
  return (
    <div className="state-block" role="alert">
      <h3>取不到这一页</h3>
      {error.auth ? (
        <p>可能是登录已经过期，或网络刚刚断开。重新载入会带你回到登录页；登录后自动回到这里。</p>
      ) : (
        <p>服务暂时没有回应：{error.message.slice(0, 120)}</p>
      )}
      <div className="state-acts">
        <button className="btn solid" onClick={() => location.reload()}>重新载入</button>
        <button className="btn" onClick={retry}>再试一次</button>
      </div>
      <p className="state-foot">{left} 秒后自动重试</p>
    </div>
  );
}

/** Data on screen, but the latest refresh failed: keep the page, say how old it is. */
export function Stale({ error, loadedAt }: { error: PollError | null; loadedAt: number }) {
  if (!error || !loadedAt) return null;
  return <div className="stale" role="status">离线 · 显示的是 {ago(new Date(loadedAt).toISOString())}的内容</div>;
}

export function Empty({ title, children, action }: { title: string; children?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="state-block">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action && <div className="state-acts">{action}</div>}
    </div>
  );
}
