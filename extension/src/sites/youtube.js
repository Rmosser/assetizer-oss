// YouTube 适配器：找视频卡片、读出条目、给出挂勾选框的位置、观看页的当前视频、播放列表。
// 2026-11 的页面：搜索、频道、首页、观看页右栏都是 yt-lockup-view-model；Shorts 是 ytm-shorts-lockup-view-model；
// 旧组件（ytd-video-renderer、ytd-rich-grid-media、ytd-playlist-video-renderer 等）留作兜底，改版时只改这里。
(() => {
  const AZ = (globalThis.AZ = globalThis.AZ || {});

  const ID = /^[A-Za-z0-9_-]{11}$/;
  // 正在直播、首播、即将开始：读不了
  const LIVE = /^(live|直播|ライブ|en vivo|en direct|upcoming|premiere|即将开始|首播|プレミア公開)(\s|$)/i;
  const CARD = [
    "yt-lockup-view-model",
    "ytm-shorts-lockup-view-model",
    "ytd-video-renderer",
    "ytd-rich-grid-media",
    "ytd-grid-video-renderer",
    "ytd-compact-video-renderer",
    "ytd-playlist-video-renderer",
    "ytd-playlist-panel-video-renderer",
    "ytd-reel-item-renderer",
  ].join(",");
  // 卡片里指向视频本身的、带缩略图的那个链接
  const THUMB = "a.ytLockupViewModelContentImage, a.shortsLockupViewModelHostEndpoint, a#thumbnail, a.reel-item-endpoint";

  function idOf(href) {
    if (!href) return null;
    let u;
    try { u = new URL(href, location.origin); } catch { return null; }
    if (!/(^|\.)youtube\.com$/.test(u.hostname) && u.hostname !== "youtu.be") return null;
    const v = u.pathname === "/watch" ? u.searchParams.get("v")
      : (u.pathname.match(/^\/shorts\/([^/?#]+)/) || u.pathname.match(/^\/live\/([^/?#]+)/) || [])[1]
      || (u.hostname === "youtu.be" ? u.pathname.slice(1) : null);
    return v && ID.test(v) ? v : null;
  }

  // 角标里的时长（旧组件把同一个时长写两遍：「10:16 10:16」）
  function seconds(text) {
    const m = (text || "").match(/(?:^|\s)(\d{1,2}(?::\d{2}){1,2})(?=\s|$)/);
    return m ? m[1].split(":").map(Number).reduce((a, b) => a * 60 + b, 0) : null;
  }
  // 按先后试几个选择器，取第一个有字的（一个选择器列表会按文档顺序取到外层容器，把两份频道名连在一起）
  const first = (card, sels) => {
    for (const s of sels) { const t = txt(card.querySelector(s)); if (t) return t; }
    return "";
  };

  const txt = (el) => (el?.textContent || "").replace(/\s+/g, " ").trim();
  const video = (id, title, channel, dur) => ({
    key: `yt:${id}`, site: "youtube", kind: "video", id,
    url: `https://www.youtube.com/watch?v=${id}`,
    title: title || null, channel: channel || null, dur: dur ?? null,
    thumb: `https://i.ytimg.com/vi/${id}/mqdefault.jpg`,
  });

  function read(card) {
    const a = card.querySelector(THUMB);
    const id = idOf(a?.getAttribute("href"));
    if (!id) return null;
    // 章节（观看页的时间点）、正在直播的、广告不收
    if (card.closest("ytd-macro-markers-list-item-renderer, ytd-ad-slot-renderer, ytd-in-feed-ad-layout-renderer, ytd-promoted-video-renderer")) return null;
    // 播放列表、合辑卡片（缩略图是一摞）：整列表从侧边栏的「这个列表」加
    if (card.querySelector("yt-collection-thumbnail-view-model, yt-collections-stack")) return null;
    const b = first(card, ["yt-thumbnail-badge-view-model", "ytd-thumbnail-overlay-time-status-renderer", "badge-shape"]);
    if (LIVE.test(b) || card.querySelector('[overlay-style="LIVE"], .badge-style-type-live-now-alternate')) return null;
    const title = first(card, [".ytLockupMetadataViewModelTitle", "#video-title", ".shortsLockupViewModelHostMetadataTitle", "h3"])
      || a.getAttribute("title") || a.getAttribute("aria-label");
    const channel = first(card, ["yt-content-metadata-view-model a", ".ytContentMetadataViewModelMetadataText", "ytd-channel-name a", "#channel-name a"]);
    return video(id, title, channel, seconds(b));
  }

  // 正在看的视频（观看页）
  function current() {
    if (location.pathname !== "/watch" && !location.pathname.startsWith("/shorts/")) return null;
    const id = idOf(location.href);
    if (!id) return null;
    const meta = document.querySelector("ytd-watch-metadata");
    const title = txt(meta?.querySelector("h1")) || document.title.replace(/ - YouTube$/, "");
    const channel = txt(meta?.querySelector("ytd-channel-name a"));
    const v = document.querySelector("video.html5-main-video");
    if (document.querySelector("#movie_player.ytp-live")) return null; // 正在直播：读不了
    const dur = v && Number.isFinite(v.duration) && v.duration > 0 && !document.querySelector(".ad-showing") ? Math.round(v.duration) : null;
    return video(id, title, channel, dur);
  }

  // 播放列表（列表页或带 list= 的观看页）：整个列表算一条，交给后端展开
  function playlist() {
    const u = new URL(location.href);
    const list = u.searchParams.get("list");
    if (!list || /^(WL|LL|RD)/.test(list)) return null; // 稍后观看、喜欢的、自动合辑：后端读不到或无穷尽
    const title = txt(document.querySelector(
      "yt-page-header-view-model h1, ytd-playlist-header-renderer #title, ytd-playlist-panel-renderer #header-description h3 a, .ytPageHeaderViewModelTitle"));
    // 期数：列表页的页头写「23 个视频」/「23 videos」，观看页的列表面板写「1 / 23」
    const head = txt(document.querySelector("yt-page-header-view-model, ytd-playlist-header-renderer"));
    const panel = txt(document.querySelector("ytd-playlist-panel-renderer #publisher-container, ytd-playlist-panel-renderer .index-message"));
    const m = head.match(/(\d[\d,]*)\s*(?:videos?|个视频|部视频|支影片|本の動画)/i) || panel.match(/\/\s*(\d[\d,]*)/);
    return {
      key: `yt-list:${list}`, site: "youtube", kind: "list", id: list,
      url: `https://www.youtube.com/playlist?list=${list}`,
      title: title || null, channel: null, dur: null, thumb: null,
      count: m ? Number(m[1].replace(/,/g, "")) : null,
    };
  }

  // 勾选框挂在缩略图上（卡片里第一个缩略图容器）
  const mount = (card) => card.querySelector(THUMB);

  AZ.sites = AZ.sites || {};
  AZ.sites.youtube = { CARD, read, current, playlist, mount, idOf, seconds };
})();
