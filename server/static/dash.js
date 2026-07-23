/* DragonRecorder library — Loom-style card grid. Editing happens on the
   watch page's owner rail; the library is for finding, sharing, deleting. */
(() => {
  const $ = (id) => document.getElementById(id);
  const fmtDur = (s) => {
    if (s == null) return "";
    s = Math.round(s);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  const rel = (iso) => {
    const d = (Date.now() - new Date(iso).getTime()) / 1000;
    if (d < 3600) return `${Math.max(1, Math.round(d / 60))} min ago`;
    if (d < 86400 * 2) return `${Math.round(d / 3600)} h ago`;
    return `${Math.round(d / 86400)} days ago`;
  };
  const daysLeft = (iso) =>
    iso ? Math.max(0, (new Date(iso) - Date.now()) / 86400000) : null;
  const toast = (msg) => {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    setTimeout(() => t.classList.remove("show"), 1800);
  };
  const guard = (r) => {
    if (r.status === 401) { location.reload(); throw new Error("unauthorized"); }
    return r;
  };

  $("logout").addEventListener("click", async () => {
    await fetch("/api/dash/logout", { method: "POST" });
    location.reload();
  });

  // ---- auto-apply (sidebar) ----
  (async () => {
    const s = await (await fetch("/api/settings/auto-apply")).json();
    document.querySelectorAll(".aa").forEach((cb) => {
      cb.checked = !!s[cb.dataset.kind];
      cb.addEventListener("change", async () => {
        await fetch("/api/dash/settings/auto-apply", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [cb.dataset.kind]: cb.checked }),
        });
        toast("Default saved");
      });
    });
  })();

  // ---- library grid ----
  const load = async () => {
    const data = await guard(await fetch("/api/dash/recordings")).json();
    const grid = $("grid");
    grid.textContent = "";
    const recs = data.recordings;
    $("emptyMsg").hidden = recs.length > 0;
    $("countPill").textContent = recs.length
      ? `${recs.length} recording${recs.length === 1 ? "" : "s"}` : "";
    for (const r of recs) {
      const card = document.createElement("div");
      card.className = "card";
      const dl = daysLeft(r.expires_at);
      const badge = r.status !== "ready"
        ? `<span class="status-badge ${r.status === "pending" ? "" : "bad"}">${r.status}</span>`
        : "";
      card.innerHTML = `
        <div class="thumbwrap">
          <img src="/media/${r.slug}/thumb.jpg" loading="lazy" alt=""
               onerror="this.remove()">
          <span class="noimg">${r.status === "pending" ? "uploading…" : ""}</span>
          ${badge}
          ${r.duration_s ? `<span class="dur num">${fmtDur(r.duration_s)}</span>` : ""}
        </div>
        <div class="cbody">
          <input class="title" value="" aria-label="Title (click to edit)">
          <div class="cmeta2">
            <span>${rel(r.created_at)}</span>
            <span class="num">${r.views} view${r.views === 1 ? "" : "s"}</span>
            ${r.comments ? `<span class="num">${r.comments} 💬</span>` : ""}
            ${dl != null && r.status === "ready"
              ? `<span class="expiry num ${dl < 3 ? "soon" : ""}">${Math.ceil(dl)}d left</span>` : ""}
          </div>
          <div class="cardbtns">
            <button class="copy">Copy link</button>
            <button class="danger del">Delete</button>
          </div>
        </div>`;
      const title = card.querySelector(".title");
      title.value = r.title || "Untitled recording";
      title.addEventListener("click", (e) => e.stopPropagation());
      title.addEventListener("change", async () => {
        await fetch(`/api/dash/recordings/${r.slug}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title.value }),
        });
        toast("Title saved");
      });
      card.querySelector(".copy").addEventListener("click", async (e) => {
        e.stopPropagation();
        await navigator.clipboard.writeText(`${location.origin}/w/${r.slug}`);
        toast("Link copied");
      });
      card.querySelector(".del").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete "${r.title || r.slug}" permanently? The link will stop working.`)) return;
        await fetch(`/api/dash/recordings/${r.slug}`, { method: "DELETE" });
        toast("Recording deleted");
        load();
      });
      card.addEventListener("click", () => (location.href = `/w/${r.slug}`));
      grid.appendChild(card);
    }
  };
  load();
})();
