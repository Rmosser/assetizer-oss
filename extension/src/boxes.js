// 交给哪台盒子：只能是 manifest 里写死的这几个（内容脚本的匹配规则不能运行时改）。
(() => {
  const AZ = (globalThis.AZ = globalThis.AZ || {});
  const BOXES = [
    { id: "prodbox", name: "prodbox", origin: "https://assetizer.prodbox.heiyu.space" },
    { id: "devbox", name: "devbox", origin: "https://assetizer.devbox.heiyu.space" },
    { id: "local", name: "本机开发", origin: "http://localhost:5173" },
    { id: "local-api", name: "本机（不经 Vite）", origin: "http://localhost:8771" },
  ];
  AZ.boxes = {
    list: BOXES,
    async current() {
      const { box } = await chrome.storage.local.get("box");
      return BOXES.find((b) => b.id === box) || BOXES[0];
    },
    set: (id) => chrome.storage.local.set({ box: id }),
  };
})();
