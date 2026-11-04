import { useEffect, useState } from "react";
import { post } from "../api";
import { ago } from "../util";
import { Sheet } from "./Sheet";

/* 机房 · YouTube 会话: setting up CookieCloud in four steps, each saying where its value comes from, then two tests.
   The secrets are write-only: the server never sends one back, the form never refills one, and nothing is kept
   outside this component's state (no storage, URL or console). */

type Field = { configured: boolean; source: "env" | "file" | null; updated_at: string | null };
type Tone = "ok" | "warn" | "bad" | "muted";
type Status = [Tone, string];
type Secret = { key: string; label: string; hint: string };

const ORIGIN = "https://cookiecloud-read.example.com";
const SETUP = "https://cookiecloud.prodbox.heiyu.space/setup";
const ALLOWLIST = `${SETUP}/read-allowlist`;
const ZERO_TRUST = "https://one.dash.cloudflare.com/";
const ACCESS: Secret[] = [
  { key: "access_client_id", label: "Access Client ID", hint: "以 .access 结尾" },
  { key: "access_client_secret", label: "Access Client Secret", hint: "一长串字母、数字和符号" },
];
const TOKEN: Secret = { key: "app_token", label: "App Token", hint: "以 ccat_ 开头" };
const SECRETS = [...ACCESS, TOKEN];
// failure codes from the last CookieCloud read that point at a step
const PLUGIN_CODES = ["sync_source_not_configured", "cookie_payload_missing", "no_youtube_cookies"];
const TOKEN_CODES = ["token_not_found", "token_revoked", "missing_scope", "domain_not_allowed"];

const fieldState = (f?: Field) =>
  f?.source === "env" ? "由环境变量提供（此处不可修改）"
    : f?.configured ? `已配置 · 文件${f.updated_at ? ` · ${ago(f.updated_at)}更新` : ""}` : "未配置";

/** How far each of the four steps is, from what the server last saw. */
export function ccSteps(yt: any): Status[] {
  const cc = yt?.cookiecloud || {};
  const f: Record<string, Field> = cc.fields || {};
  const st = yt?.state?.source === "cookiecloud" ? yt.state : null;
  const code: string | null = st && !st.ok ? st.code : null;
  const vt = yt?.video_test?.verdict ? yt.video_test : null;
  const plugin: Status = vt?.verdict === "not_signed_in" ? ["bad", "同步来的 cookies 没有登录 YouTube"]
    : code && PLUGIN_CODES.includes(code) ? ["bad", "还没同步到 YouTube 的 cookies"]
    : st?.ok ? ["ok", `已同步 ${st.count} 条`] : ["muted", "做第 4 步的测试时确认"];
  const missing = [!cc.origin && "origin", !f.access_client_id?.configured && "Client ID",
    !f.access_client_secret?.configured && "Client Secret"].filter(Boolean);
  const access: Status = code === "access_denied" ? ["bad", "Cloudflare 拒绝了，检查这两项"]
    : !missing.length ? ["ok", "已填写"] : missing.length === 3 ? ["muted", "未填写"] : ["warn", `还差 ${missing.join("、")}`];
  const token: Status = code && TOKEN_CODES.includes(code) ? ["bad", "这个令牌不能用，重新建一个"]
    : f.app_token?.configured ? ["ok", "已填写"] : ["muted", "未填写"];
  const verify: Status = vt ? (vt.ok ? ["ok", "视频测试通过"] : ["bad", "视频测试没通过"])
    : st?.ok ? ["warn", "还差视频测试"] : ["muted", "未测试"];
  return [plugin, access, token, verify];
}

/** 清除 asks twice: the first press arms it for a few seconds. */
function ClearBtn({ onClear, disabled }: { onClear: () => void; disabled?: boolean }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button type="button" className={`btn sm${armed ? " armed" : ""}`} disabled={disabled}
      onClick={() => { if (armed) { setArmed(false); onClear(); } else setArmed(true); }}>
      {armed ? "确认清除" : "清除"}
    </button>
  );
}

function Step({ n, title, status, children }: { n: number; title: string; status: Status; children: React.ReactNode }) {
  return (
    <li className="cc-step">
      <div className="cc-step-head">
        <span className={`cc-n ${status[0]}`} aria-hidden>{status[0] === "ok" ? "✓" : n}</span>
        <h4>{title}</h4>
        <span className={`chip ${status[0]}`}>{status[1]}</span>
      </div>
      <div className="cc-step-body">{children}</div>
    </li>
  );
}

function SecretField({ f, st, value, busy, onChange, onClear }: {
  f: Secret; st?: Field; value: string; busy: boolean; onChange: (v: string) => void; onClear: () => void;
}) {
  const env = st?.source === "env";
  return (
    <div className="cc-field">
      <div className="cc-head"><label htmlFor={`cc-${f.key}`}>{f.label}</label><span className={`cc-st${env ? " env" : ""}`}>{fieldState(st)}</span></div>
      <div className="cc-row">
        <input id={`cc-${f.key}`} type="password" autoComplete="off" spellCheck={false} autoCapitalize="off" autoCorrect="off"
          data-1p-ignore value={value} disabled={env || busy}
          placeholder={env ? "由服务器环境变量提供" : st?.configured ? "留空表示不修改" : f.hint}
          onChange={(e) => onChange(e.target.value)} />
        {st?.source === "file" && <ClearBtn onClear={onClear} disabled={busy} />}
      </div>
    </div>
  );
}

function ConnectionResult({ r }: { r: any }) {
  const tok = r.token;
  return (
    <div className={`cc-result${r.ok ? "" : " bad"}`} role="status">
      {r.ok ? <>已读到 <span className="num">{r.count}</span> 条{r.hosts?.length ? `（${r.hosts.join("、")}）` : ""}</>
        : <>{r.fix}<span className="muted">（错误码 {r.code}{r.detail ? ` · ${r.detail}` : ""}）</span></>}
      {tok && <div className="cc-tok">令牌 {tok.name || "—"} · 域名白名单 {tok.allowed_domains?.length ? tok.allowed_domains.join("、") : "—"}</div>}
    </div>
  );
}

function VideoResult({ r }: { r: any }) {
  const v = r.video;
  return (
    <div className={`cc-result${r.ok ? "" : " bad"}`} role="status">
      {r.message}
      {v?.title && <div className="cc-tok">《{v.title}》{v.channel ? ` · ${v.channel}` : ""}{v.availability === "subscriber_only" ? " · 会员视频" : ""}</div>}
      {r.woken > 0 && <div className="cc-tok">等待登录的 {r.woken} 个视频已重新排队。</div>}
      {!r.ok && r.detail && <div className="cc-said">{r.verdict === "cookiecloud" ? "原因" : "YouTube 的原话"}：{r.detail}</div>}
      {r.code && <div className="cc-said">错误码 {r.code}</div>}
      {r.at && <div className="cc-said">{ago(r.at)}测试</div>}
    </div>
  );
}

export function CookieCloudSheet({ yt: polled, onClose, reload }: { yt: any; onClose: () => void; reload: () => void }) {
  const [yt, setYt] = useState(polled);
  useEffect(() => setYt(polled), [polled]);
  const cc = yt?.cookiecloud;
  const fields: Record<string, Field> = cc?.fields || {};
  const [origin, setOrigin] = useState<string>(cc?.origin || ORIGIN);
  const [vals, setVals] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"save" | "clear" | "test" | "video" | null>(null);
  const [msg, setMsg] = useState<{ bad: boolean; text: string } | null>(null);
  const [test, setTest] = useState<any>(null);
  const [video, setVideo] = useState<string>(yt?.video_test?.url || "");
  const [vmsg, setVmsg] = useState<string | null>(null);
  // a step's help starts open while that step is not done (and stays as the user leaves it)
  const [helpOpen] = useState(() => ccSteps(polled).map((s) => s[0] !== "ok"));
  const steps = ccSteps(yt);
  const vt = yt?.video_test?.verdict ? yt.video_test : null;
  const blocked = yt?.state?.source === "cookiecloud" && yt.state.code === "ip_not_allowed" ? yt.state : null;
  const originDirty = !!origin.trim() && origin.trim().replace(/\/+$/, "") !== (cc?.origin || "");
  const dirty = originDirty || SECRETS.some((f) => (vals[f.key] || "").trim());
  const fail = (e: unknown) => setMsg({ bad: true, text: String((e as Error).message || e) });
  const setVal = (key: string) => (v: string) => setVals((x) => ({ ...x, [key]: v }));

  const save = async () => {
    const body: Record<string, string> = {};
    if (originDirty) body.origin = origin.trim();
    for (const f of SECRETS) if ((vals[f.key] || "").trim()) body[f.key] = vals[f.key];
    setBusy("save"); setMsg(null); setTest(null);
    try {
      const r = await post("/api/cookiecloud/credentials", body);
      setYt((p: any) => ({ ...p, ...r }));
      setVals({});
      setOrigin(r.cookiecloud?.origin || "");
      setMsg({ bad: false, text: "已保存。接着做第 4 步：先「测试连接」，再「视频测试」。" });
      reload();
    } catch (e) { fail(e); }
    setBusy(null);
  };
  const clear = async (key: string) => {
    setBusy("clear"); setMsg(null); setTest(null);
    try {
      const r = await post("/api/cookiecloud/credentials", { clear: [key] });
      setYt((p: any) => ({ ...p, ...r }));
      if (key === "origin") setOrigin("");
      setVals((v) => ({ ...v, [key]: "" }));
      setMsg({ bad: false, text: "已清除。" });
      reload();
    } catch (e) { fail(e); }
    setBusy(null);
  };
  const runTest = async () => {
    setBusy("test"); setMsg(null);
    try { setTest(await post("/api/cookiecloud/test")); reload(); } catch (e) { fail(e); }  // a 429 keeps the last result
    setBusy(null);
  };
  const runVideo = async () => {
    setBusy("video"); setVmsg(null);
    try {
      const r = await post("/api/cookiecloud/test-video", { url: video.trim() });
      setYt((p: any) => ({ ...p, video_test: r }));
      reload();
    } catch (e) { setVmsg(String((e as Error).message || e)); }
    setBusy(null);
  };

  return (
    <Sheet title="配置 CookieCloud" onClose={onClose}>
      <p className="sheet-lede">让服务器用你 Chrome 里的 YouTube 登录态下载视频，会员视频也一样。按顺序做完下面 4 步。密钥只写不读：保存后不再显示，留空表示不修改。</p>
      {blocked && <p className="cc-msg bad" role="status">{blocked.detail?.startsWith("出口 IP") ? blocked.detail : "出口 IP"} 不在白名单，令牌不用改。<a href={ALLOWLIST} target="_blank" rel="noreferrer">打开白名单 ↗</a></p>}
      <ol className="cc-steps">
        <Step n={1} title="配对 Chrome 插件" status={steps[0]}>
          <p className="cc-why">让 Chrome 把你的登录态同步到 CookieCloud（prodbox 那一台）。</p>
          <details className="cc-help" open={helpOpen[0]}>
            <summary>怎么做</summary>
            <ol className="cc-how">
              <li>打开 <a href={SETUP} target="_blank" rel="noreferrer">CookieCloud Setup ↗</a>，在「步骤 1：生成插件连接码」点「生成连接码」，复制它。连接码只能用一次，24 小时内有效。</li>
              <li>打开 Chrome 里 CookieCloud 插件的设置页，粘贴到「连接码快速配置」，点「导入」。插件会马上同步一次。</li>
              <li>确认 Chrome 里登录 YouTube 的是你买了会员的账号，然后在插件里点「立即同步」。</li>
            </ol>
          </details>
        </Step>

        <Step n={2} title="填写 Access 服务令牌" status={steps[1]}>
          <p className="cc-why">Cloudflare 用它确认请求来自课代表的服务器。Origin 一般不用改。</p>
          <div className="sheet-form cc-form">
            <div className="cc-field">
              <div className="cc-head"><label htmlFor="cc-origin">Origin</label><span className="cc-st">{cc?.origin ? "已配置" : "未配置"}</span></div>
              <div className="cc-row">
                <input id="cc-origin" value={origin} onChange={(e) => setOrigin(e.target.value)} inputMode="url"
                  autoComplete="off" spellCheck={false} autoCapitalize="off" disabled={!!busy} />
                {cc?.origin && <ClearBtn onClear={() => clear("origin")} disabled={!!busy} />}
              </div>
            </div>
            {ACCESS.map((f) => (
              <SecretField key={f.key} f={f} st={fields[f.key]} value={vals[f.key] || ""} busy={!!busy}
                onChange={setVal(f.key)} onClear={() => clear(f.key)} />
            ))}
          </div>
          <details className="cc-help" open={helpOpen[1]}>
            <summary>这两项从哪里拿</summary>
            <p>在 <a href={ZERO_TRUST} target="_blank" rel="noreferrer">Cloudflare Zero Trust ↗</a> 的「Access controls」→「Service credentials」新建一个服务令牌，复制 Client ID 和 Client Secret（Secret 只显示一次），再把这个令牌加进 CookieCloud 只读入口那个 Access 应用的策略。已填写的令牌 2027-10-13 到期，没过期就不用换。</p>
          </details>
        </Step>

        <Step n={3} title="填写 App Token" status={steps[2]}>
          <p className="cc-why">CookieCloud 的只读令牌，只能读 youtube.com 的 cookies。</p>
          <div className="sheet-form cc-form">
            <SecretField f={TOKEN} st={fields[TOKEN.key]} value={vals[TOKEN.key] || ""} busy={!!busy}
              onChange={setVal(TOKEN.key)} onClear={() => clear(TOKEN.key)} />
          </div>
          <details className="cc-help" open={helpOpen[2]}>
            <summary>从哪里拿</summary>
            <p>打开 <a href={SETUP} target="_blank" rel="noreferrer">CookieCloud Setup ↗</a>，在「步骤 3：外部应用访问令牌」里：名称随便填（比如「课代表」），域名只填一行 <code>youtube.com</code>，点「创建只读令牌」。令牌只显示一次，复制后直接粘贴到上面。以前建过但手里没有它的值，就把旧的「吊销」，再建一个。</p>
          </details>
          <div className="cc-save">
            <button type="button" className="btn solid" onClick={save} disabled={!!busy || !dirty}>{busy === "save" ? "保存中…" : "保存第 2、3 步"}</button>
            {msg && <p className={`cc-msg${msg.bad ? " bad" : ""}`} role="status">{msg.text}</p>}
          </div>
        </Step>

        <Step n={4} title="测试" status={steps[3]}>
          {dirty && <p className="cc-note">测试用的是已保存的内容：先保存，再测试。</p>}
          <div className="cc-test">
            <p className="cc-why"><strong>测试连接</strong>：能不能从 CookieCloud 读到 youtube.com 的 cookies。</p>
            <div><button type="button" className="btn" onClick={runTest} disabled={!!busy || dirty}>{busy === "test" ? "测试中…" : "测试连接"}</button></div>
            {test && <ConnectionResult r={test} />}
          </div>
          <div className="cc-test">
            <p className="cc-why"><strong>视频测试</strong>：YouTube 认不认这份登录态。填一个视频链接，最好是你买了会员的频道里的会员视频：服务器只用 CookieCloud 的登录态去读它（不下载），读得到就说明登录态有效。通过后，等待登录的视频会立刻重试。</p>
            <div className="sheet-form cc-row">
              <input value={video} onChange={(e) => setVideo(e.target.value)} inputMode="url" autoComplete="off" spellCheck={false}
                autoCapitalize="off" placeholder="https://www.youtube.com/watch?v=…" aria-label="测试用的 YouTube 视频链接" disabled={!!busy} />
              <button type="button" className="btn" onClick={runVideo} disabled={!!busy || dirty || !video.trim()}>
                {busy === "video" ? "测试中…" : "视频测试"}
              </button>
            </div>
            {busy === "video" && <p className="cc-note">服务器正在用这份登录态读视频，大约要半分钟。</p>}
            {vmsg && <p className="cc-msg bad" role="status">{vmsg}</p>}
            {vt && <VideoResult r={vt} />}
          </div>
        </Step>
      </ol>
    </Sheet>
  );
}
