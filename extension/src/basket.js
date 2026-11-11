// 选片篮：跨搜索、跨标签页、跨窗口共享，存在 chrome.storage.local（关浏览器也在）。
// 条目只存规范链接和展示用的信息，不分平台：{key, site, kind, id, url, title, channel, dur, thumb, at}
// kind: "video" | "list"；dur: 秒，读不到为 null。
(() => {
  const AZ = (globalThis.AZ = globalThis.AZ || {});
  const KEY = "basket";
  const MAX = 200; // 选片抽屉一次最多 100 期；篮子多留一些，送的时候再截

  const read = async () => ((await chrome.storage.local.get(KEY))[KEY] || []);
  const write = (items) => chrome.storage.local.set({ [KEY]: items });

  // 同一时刻几个标签页一起改：串起来，读改写不交错（只在本页内串；跨页靠 storage 的最后写入，条目按 key 合并）
  let chain = Promise.resolve();
  const edit = (fn) => (chain = chain.then(async () => {
    const before = await read();
    const after = fn(before);
    if (after !== before) await write(after);
    return after;
  }));

  AZ.basket = {
    all: read,
    add: (items) => edit((list) => {
      const have = new Set(list.map((x) => x.key));
      const fresh = items.filter((x) => x && !have.has(x.key)).map((x) => ({ ...x, at: Date.now() }));
      return fresh.length ? [...list, ...fresh].slice(-MAX) : list;
    }),
    remove: (keys) => edit((list) => {
      const out = new Set(keys);
      const left = list.filter((x) => !out.has(x.key));
      return left.length === list.length ? list : left;
    }),
    toggle: (item) => edit((list) => (list.some((x) => x.key === item.key)
      ? list.filter((x) => x.key !== item.key)
      : [...list, { ...item, at: Date.now() }].slice(-MAX))),
    clear: () => edit((list) => (list.length ? [] : list)),
    onChange: (fn) => chrome.storage.onChanged.addListener((ch, area) => {
      if (area === "local" && ch[KEY]) fn(ch[KEY].newValue || []);
    }),
    MAX,
  };

  // 交接：送出的一批记在 sent 里（批次 id、条目、时间）；课代表页面领走时记下 claimed，这才算送到（侧边栏这才标「已送出」）。
  // 页面确认后只移出它说的那些；30 分钟没确认就当没送出。
  // 篮子没变时再送沿用同一个批次 id：课代表按批次 id 认，同一批不会建出第二个专题。
  const SENT = "sent";
  const WAIT = 30 * 60 * 1000;
  const sentNow = async () => {
    const s = (await chrome.storage.local.get(SENT))[SENT];
    return s && Date.now() - s.at < WAIT ? s : null;
  };
  const rid = () => [...crypto.getRandomValues(new Uint8Array(12))].map((b) => b.toString(36).padStart(2, "0")).join("").slice(0, 16);
  AZ.handoff = {
    WAIT,
    pending: sentNow,
    async send() {
      const items = await read();
      if (!items.length) return null;
      const keys = items.map((x) => x.key);
      const old = (await chrome.storage.local.get(SENT))[SENT];
      const same = old && old.keys.length === keys.length && old.keys.every((k, i) => k === keys[i]);
      const s = { batch: same ? old.batch : rid(), keys, at: Date.now() };
      await chrome.storage.local.set({ [SENT]: s });
      return s;
    },
    // 页面来要：带批次 id 的只认那一批；不带的（登录跳转丢了 #pick）给 30 分钟内没送到的那一批
    async claim(batch) {
      const s = await sentNow();
      if (!s || (batch && batch !== s.batch)) return null;
      const want = new Set(s.keys);
      const items = (await read()).filter((x) => want.has(x.key));
      if (!items.length) return null;
      if (!s.claimed) await chrome.storage.local.set({ [SENT]: { ...s, claimed: Date.now() } });
      return { batch: s.batch, items };
    },
    async done(batch, taken, dropped) {
      const s = (await chrome.storage.local.get(SENT))[SENT];
      if (!s || s.batch !== batch) return;
      const ok = (k) => typeof k === "string" && s.keys.includes(k);
      await AZ.basket.remove([...(taken || []), ...(dropped || [])].filter(ok));
      await chrome.storage.local.remove(SENT);
    },
  };

  // 合计时长：读不到的算未知，合计前写「约」
  AZ.total = (items) => {
    let s = 0, unknown = 0;
    for (const x of items) (x.dur == null ? unknown++ : (s += x.dur));
    return { seconds: s, unknown };
  };
  // 与课代表的 hours() 同一个口径：不到一小时写分钟，否则一位小数的小时
  AZ.hours = (sec) => {
    const h = sec / 3600;
    if (!h) return "";
    return h < 1 ? `${Math.max(1, Math.round(h * 60))} 分钟` : `${Math.round(h * 10) / 10} 小时`;
  };
  AZ.clock = (sec) => {
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  };
})();
