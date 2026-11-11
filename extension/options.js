const AZ = globalThis.AZ;
(async () => {
  const now = await AZ.boxes.current();
  const f = document.getElementById("boxes");
  for (const b of AZ.boxes.list) {
    const l = document.createElement("label");
    l.innerHTML = `<input type="radio" name="box" value="${b.id}"${b.id === now.id ? " checked" : ""}><span>${b.name}</span><small>${new URL(b.origin).host}</small>`;
    l.querySelector("input").addEventListener("change", () => AZ.boxes.set(b.id));
    f.append(l);
  }
})();
