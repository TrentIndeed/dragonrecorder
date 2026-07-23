/* DragonRecorder dashboard — single user, no framework. */
(() => {
  const $ = (id) => document.getElementById(id);
  const fmtDur = (s) => {
    if (s == null) return "—";
    s = Math.round(s);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  const fmtSize = (b) => {
    if (!b) return "—";
    if (b > 1e9) return (b / 1e9).toFixed(1) + " GB";
    return (b / 1e6).toFixed(0) + " MB";
  };
  const fmtDate = (iso) => new Date(iso).toLocaleDateString(undefined,
    { month: "short", day: "numeric" });
  const daysLeft = (iso) => {
    if (!iso) return null;
    return Math.max(0, (new Date(iso) - Date.now()) / 86400000);
  };
  const toast = (msg) => {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    setTimeout(() => t.classList.remove("show"), 1800);
  };

  const EDIT_LABELS = {
    fillers: ["Remove filler words", "filler word"],
    silences: ["Remove silences", "silence"],
    captions: ["Captions", "caption block"],
  };

  // session expired → back to the login card
  const guard = (r) => {
    if (r.status === 401) { location.reload(); throw new Error("unauthorized"); }
    return r;
  };

  document.getElementById("logout")?.addEventListener("click", async () => {
    await fetch("/api/dash/logout", { method: "POST" });
    location.reload();
  });

  // ---- auto-apply settings ----
  const loadAutoApply = async () => {
    const s = await (await fetch("/api/settings/auto-apply")).json();
    document.querySelectorAll("#autoApply input").forEach((cb) => {
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
  };

  // ---- list ----
  const load = async () => {
    const data = await guard(await fetch("/api/dash/recordings")).json();
    const list = $("list");
    list.textContent = "";
    $("emptyMsg").hidden = data.recordings.length > 0;
    for (const r of data.recordings) {
      const row = document.createElement("div");
      row.className = "row";
      const dl = daysLeft(r.expires_at);
      const statusBit = r.status !== "ready"
        ? `<span class="status-${r.status}">${r.status}</span>`
        : `<span class="expiry num ${dl < 3 ? "soon" : ""}">${dl == null ? "" : Math.ceil(dl) + "d left"}</span>`;
      row.innerHTML = `
        <img class="thumb" src="/media/${r.slug}/thumb.jpg"
             onerror="this.style.visibility='hidden'" alt="">
        <div class="titlebox">
          <input class="title" value="" aria-label="Title (click to edit)">
          <div class="sub">${fmtDate(r.created_at)} ${r.title_is_ai && r.title ? "· <span style='color:var(--ai)'>AI title</span>" : ""}</div>
        </div>
        <span class="stat num">${fmtDur(r.duration_s)}</span>
        <span class="stat num">${fmtSize(r.size_bytes)}</span>
        <span class="stat num"><b>${r.views}</b> views</span>
        <span class="stat num">${r.comments} 💬</span>
        <span class="stat">${statusBit}</span>
        <div class="rowbtns">
          <button class="copy">Copy link</button>
          <button class="open">Open</button>
          <button class="danger del">Delete</button>
        </div>`;
      const titleInput = row.querySelector(".title");
      titleInput.value = r.title || "Untitled recording";
      titleInput.addEventListener("change", async () => {
        await fetch(`/api/dash/recordings/${r.slug}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: titleInput.value }),
        });
        toast("Title saved");
      });
      row.querySelector(".copy").addEventListener("click", async () => {
        await navigator.clipboard.writeText(`${location.origin}/w/${r.slug}`);
        toast("Link copied");
      });
      row.querySelector(".open").addEventListener("click", () =>
        window.open(`/w/${r.slug}`, "_blank"));
      row.querySelector(".del").addEventListener("click", async () => {
        if (!confirm(`Delete "${r.title || r.slug}" permanently? The link will stop working.`)) return;
        await fetch(`/api/dash/recordings/${r.slug}`, { method: "DELETE" });
        toast("Recording deleted");
        load();
      });
      row.querySelector(".thumb").addEventListener("click", () => openDetail(r.slug));
      row.querySelector(".titlebox .sub").addEventListener("click", () => openDetail(r.slug));
      list.appendChild(row);
    }
  };

  // ---- detail ----
  const openDetail = async (slug) => {
    const d = await (await fetch(`/api/dash/recordings/${slug}`)).json();
    $("dTitle").textContent = d.recording.title || "Untitled recording";

    // edits panel: every detector shown, zero states greyed but visible
    const editsDiv = $("dEdits");
    editsDiv.textContent = "";
    const byKind = Object.fromEntries(d.edits.map((e) => [e.kind, e]));
    for (const kind of ["fillers", "silences", "captions"]) {
      const e = byKind[kind];
      const [label, unit] = EDIT_LABELS[kind];
      const div = document.createElement("div");
      const count = e ? e.count : null;
      div.className = "edit-toggle" + (!e || !e.count ? " zero" : "");
      div.innerHTML = `
        <input type="checkbox" id="et-${kind}" ${e?.enabled ? "checked" : ""}
               ${!e || !e.count ? "disabled" : ""}>
        <label for="et-${kind}">${label}</label>
        <span class="count num">${
          e == null ? "not analyzed yet"
          : `${count} ${unit}${count === 1 ? "" : "s"} found`}</span>
        <span class="note">${
          e && e.count && kind !== "captions" && !e.has_render && e.enabled
            ? "render pending — the recorder will pick this up" : ""}</span>`;
      const cb = div.querySelector("input");
      cb.addEventListener("change", async () => {
        await fetch(`/api/dash/recordings/${slug}/edits/${kind}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: cb.checked }),
        });
        toast(cb.checked ? `${label}: on` : `${label}: off — original restored`);
        openDetail(slug);
      });
      editsDiv.appendChild(div);
    }

    // viewers table
    const tb = $("dViewers").querySelector("tbody");
    tb.textContent = "";
    for (const v of d.viewers) {
      const tr = document.createElement("tr");
      const dur = d.recording.duration_s;
      tr.innerHTML = `<td></td><td>${new Date(v.started_at).toLocaleString()}</td>
        <td class="num">${fmtDur(v.watched_s)}</td>
        <td class="num">${dur ? Math.round((v.max_pos_s / dur) * 100) + "%" : "—"}</td>`;
      tr.children[0].textContent =
        (v.label || v.viewer_id.slice(0, 6)) + (v.is_owner ? " (you)" : "");
      tb.appendChild(tr);
    }

    // drop-off histogram
    const heat = await (await fetch(`/api/w/${slug}/heatmap`)).json();
    const cv = $("dropoff");
    const ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cv.width, cv.height);
    const max = Math.max(1, ...heat.buckets);
    const bw = cv.width / 100;
    for (let i = 0; i < 100; i++) {
      const h = (heat.buckets[i] / max) * (cv.height - 4);
      ctx.fillStyle = "rgba(167, 139, 250, 0.8)";
      ctx.fillRect(i * bw, cv.height - h, bw - 1, h);
    }

    $("dTranscriptWrap").hidden = !d.recording.transcript;
    $("dTranscript").textContent = d.recording.transcript || "";
    $("detail").showModal();
  };
  $("dClose").addEventListener("click", () => $("detail").close());

  loadAutoApply();
  load();
})();
