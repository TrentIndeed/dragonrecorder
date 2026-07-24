/* DragonRecorder player — no framework, Chrome-first. */
(() => {
  const $ = (id) => document.getElementById(id);
  const video = $("video");
  const slug = window.DR.slug;
  const fmt = (s) => {
    s = Math.max(0, Math.floor(s));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  };

  // relative timestamps
  const rel = (iso) => {
    const d = (Date.now() - new Date(iso).getTime()) / 1000;
    if (d < 90) return "just now";
    if (d < 3600) return `${Math.round(d / 60)} min ago`;
    if (d < 86400 * 2) return `${Math.round(d / 3600)} h ago`;
    return `${Math.round(d / 86400)} days ago`;
  };
  document.querySelectorAll("time[data-iso]").forEach((t) => {
    if (t.dataset.iso) t.textContent = rel(t.dataset.iso);
  });

  // ---- playback ----
  const preplay = $("preplay");
  const controls = $("controls");
  // AI-chosen default speed (from the speaker's measured WPM) leads the cycle
  const aiSpeed = window.DR.defaultSpeed || 1.0;
  const baseSpeeds = [1, 1.25, 1.5, 1.75, 2];
  const speeds = [...new Set([aiSpeed, ...baseSpeeds])].sort((a, b) => a - b);
  let speedIdx = speeds.indexOf(aiSpeed);
  const showSpeed = (x) => {
    const label = `${(+x.toFixed(2))}×`;
    $("speedBtn").textContent = label;
    $("preSpeed").textContent = label;
  };
  // apply the AI default before first play
  video.playbackRate = aiSpeed;
  showSpeed(aiSpeed);

  const setPlayingUI = (playing) => {
    $("icoPlay").hidden = playing;
    $("icoPause").hidden = !playing;
    $("playBtn").setAttribute("aria-label", playing ? "Pause" : "Play");
  };
  const toggle = () => (video.paused ? video.play() : video.pause());

  $("bigplay").addEventListener("click", () => video.play());
  $("playBtn").addEventListener("click", toggle);
  video.addEventListener("click", toggle);
  video.addEventListener("play", () => {
    preplay.classList.add("hidden");
    setPlayingUI(true);
  });
  video.addEventListener("pause", () => setPlayingUI(false));
  video.addEventListener("loadedmetadata", () => {
    $("tDur").textContent = fmt(video.duration);
    if (!window.DR.duration) $("preDur").textContent = fmt(video.duration);
  });

  $("speedBtn").addEventListener("click", () => {
    speedIdx = (speedIdx + 1) % speeds.length;
    video.playbackRate = speeds[speedIdx];
    showSpeed(speeds[speedIdx]);
  });

  const ccBtn = $("ccBtn");
  if (ccBtn) {
    const track = video.textTracks[0];
    const apply = (on) => {
      if (track) track.mode = on ? "showing" : "hidden";
      ccBtn.setAttribute("aria-pressed", String(on));
    };
    apply(ccBtn.getAttribute("aria-pressed") === "true");
    ccBtn.addEventListener("click", () =>
      apply(ccBtn.getAttribute("aria-pressed") !== "true"));
  }

  $("fsBtn").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else $("stage").requestFullscreen();
  });

  // auto-hide controls while playing
  let hideTimer;
  const stage = $("stage");
  const poke = () => {
    controls.classList.remove("faded");
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (!video.paused) controls.classList.add("faded");
    }, 2500);
  };
  stage.addEventListener("mousemove", poke);
  video.addEventListener("play", poke);
  video.addEventListener("pause", () => controls.classList.remove("faded"));

  // keyboard
  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    if (e.key === " " || e.key === "k") { e.preventDefault(); toggle(); }
    if (e.key === "ArrowLeft") video.currentTime -= 5;
    if (e.key === "ArrowRight") video.currentTime += 5;
    if (e.key === "f") $("fsBtn").click();
  });

  // ---- scrub bar with attention histogram ----
  const scrub = $("scrub");
  const heat = $("heat");
  let heatData = null;

  const drawHeat = () => {
    const w = (heat.width = scrub.clientWidth * devicePixelRatio);
    const h = (heat.height = 18 * devicePixelRatio);
    const ctx = heat.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    const buckets = heatData?.viewers ? heatData.buckets : new Array(100).fill(0);
    const max = Math.max(1, ...buckets);
    const bw = w / 100;
    for (let i = 0; i < 100; i++) {
      const frac = buckets[i] / max;
      const bh = Math.max(2 * devicePixelRatio, frac * h);
      ctx.fillStyle = heatData?.viewers
        ? `rgba(167, 139, 250, ${0.25 + 0.55 * frac})`
        : "rgba(255, 255, 255, 0.22)";
      ctx.fillRect(i * bw, h - bh, bw - devicePixelRatio, bh);
    }
  };
  fetch(`/api/w/${slug}/heatmap`).then((r) => r.json()).then((d) => {
    heatData = d;
    drawHeat();
  }).catch(() => drawHeat());
  new ResizeObserver(drawHeat).observe(scrub);

  const seekTo = (clientX) => {
    const rect = scrub.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    video.currentTime = frac * (video.duration || 0);
  };
  let dragging = false;
  scrub.addEventListener("pointerdown", (e) => {
    dragging = true;
    scrub.setPointerCapture(e.pointerId);
    seekTo(e.clientX);
  });
  scrub.addEventListener("pointermove", (e) => dragging && seekTo(e.clientX));
  scrub.addEventListener("pointerup", () => (dragging = false));
  scrub.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") video.currentTime -= 5;
    if (e.key === "ArrowRight") video.currentTime += 5;
  });

  video.addEventListener("timeupdate", () => {
    const frac = video.duration ? video.currentTime / video.duration : 0;
    $("played").style.width = `${frac * 100}%`;
    $("knob").style.left = `${frac * 100}%`;
    $("tCur").textContent = fmt(video.currentTime);
    scrub.setAttribute("aria-valuenow", Math.round(frac * 100));
    const pinT = $("cPinTime");
    if (pinT) pinT.textContent = fmt(video.currentTime);
  });

  // ---- analytics: real watched ranges ----
  let rangeStart = null;
  let pending = [];
  const closeRange = () => {
    if (rangeStart !== null && video.currentTime > rangeStart + 0.4) {
      pending.push([rangeStart, video.currentTime]);
    }
    rangeStart = null;
  };
  video.addEventListener("play", () => (rangeStart = video.currentTime));
  video.addEventListener("pause", closeRange);
  video.addEventListener("seeking", () => { closeRange(); });
  video.addEventListener("seeked", () => { if (!video.paused) rangeStart = video.currentTime; });
  video.addEventListener("ended", closeRange);

  // play-count + time-to-first-play (engagement signal for the owner)
  let pendingPlays = 0;
  let firstPlayS = null;
  const pageOpen = performance.now();
  video.addEventListener("play", () => {
    pendingPlays += 1;
    if (firstPlayS === null) firstPlayS = (performance.now() - pageOpen) / 1000;
  });

  const flush = (beacon = false) => {
    closeRange();
    if (!video.paused) rangeStart = video.currentTime;
    if ((!pending.length && !pendingPlays) || window.DR.isOwner) {
      pending = []; pendingPlays = 0; return;
    }
    const payload = JSON.stringify({
      ranges: pending, plays: pendingPlays,
      first_play_s: firstPlayS,
    });
    pending = [];
    pendingPlays = 0;
    if (beacon && navigator.sendBeacon) {
      navigator.sendBeacon(`/api/w/${slug}/progress`,
        new Blob([payload], { type: "application/json" }));
    } else {
      fetch(`/api/w/${slug}/progress`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
      }).catch(() => {});
    }
  };
  setInterval(() => flush(false), 10000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush(true);
  });
  window.addEventListener("pagehide", () => flush(true));

  // ---- reactions ----
  document.querySelectorAll(".react").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const res = await fetch(`/api/w/${slug}/reactions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ emoji: btn.dataset.emoji }),
      });
      if (!res.ok) return;
      const data = await res.json();
      btn.classList.toggle("mine", data.toggled);
      document.querySelectorAll(".react").forEach((b) => {
        const c = data.counts[b.dataset.emoji] || "";
        b.querySelector(".count").textContent = c;
      });
    });
  });

  // ---- comments ----
  const seekBtns = (root) =>
    root.querySelectorAll(".tstamp").forEach((b) =>
      b.addEventListener("click", () => {
        video.currentTime = parseFloat(b.dataset.t);
        video.play();
      }));
  seekBtns(document);

  $("commentForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = $("cBody").value.trim();
    if (!body) return;
    const at_s = $("cPin").checked ? video.currentTime : null;
    const res = await fetch(`/api/w/${slug}/comments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body, author: $("cAuthor").value, at_s }),
    });
    if (!res.ok) return;
    const c = await res.json();
    $("noComments")?.remove();
    const div = document.createElement("div");
    div.className = "comment";
    div.innerHTML = `<div class="cmeta"><strong></strong>${
      c.at_s != null ? `<button class="tstamp num" data-t="${c.at_s}">${fmt(c.at_s)}</button>` : ""
    }<time>just now</time></div><p></p>`;
    div.querySelector("strong").textContent = c.author;
    div.querySelector("p").textContent = c.body;
    $("commentList").appendChild(div);
    seekBtns(div);
    $("cBody").value = "";
    $("cPin").checked = false;
  });

  // ---- owner rail (edit toggles + activity), Loom's video-page layout ----
  const EDIT_LABELS = {
    fillers: ["Remove filler words", "filler word"],
    silences: ["Remove silences", "silence"],
    captions: ["Stylized captions", "caption block"],
  };

  const buildRail = async () => {
    const me = await fetch("/api/dash/me").catch(() => null);
    if (!me || !me.ok) return;
    document.body.classList.add("owner");
    $("ownerRail").hidden = false;

    // tabs
    document.querySelectorAll(".rail .tab").forEach((t) =>
      t.addEventListener("click", () => {
        document.querySelectorAll(".rail .tab").forEach((x) =>
          x.classList.toggle("active", x === t));
        document.querySelectorAll(".tabpane").forEach((p) =>
          (p.hidden = p.id !== `pane-${t.dataset.tab}`));
      }));

    // move the transcript into its rail tab
    const ts = document.getElementById("transcriptSection");
    if (ts) {
      document.getElementById("pane-transcript").appendChild(ts);
      ts.classList.add("in-rail");
    } else {
      document.getElementById("pane-transcript").innerHTML =
        '<p class="empty">No transcript for this recording.</p>';
    }

    const refresh = async () => {
      const d = await (await fetch(`/api/dash/recordings/${slug}`)).json();
      const box = $("railEdits");
      box.textContent = "";
      const byKind = Object.fromEntries(d.edits.map((e) => [e.kind, e]));
      for (const kind of ["fillers", "silences", "captions"]) {
        const e = byKind[kind];
        const [label, unit] = EDIT_LABELS[kind];
        const row = document.createElement("label");
        row.className = "edit-row" + (!e || !e.count ? " zero" : "");
        const pendingNote = e && e.count && kind !== "captions" && e.enabled && !e.has_render
          ? '<span class="note">render pending — the recorder picks this up</span>' : "";
        row.innerHTML = `
          <input type="checkbox" ${e?.enabled ? "checked" : ""}
                 ${!e || !e.count ? "disabled" : ""}>
          <span>${label}${pendingNote}</span>
          <span class="cnt num">${e == null ? "not analyzed"
            : `${e.count} found`}</span>`;
        row.querySelector("input").addEventListener("change", async (ev) => {
          await fetch(`/api/dash/recordings/${slug}/edits/${kind}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: ev.target.checked }),
          });
          refresh();
        });
        box.appendChild(row);
      }

      // ---- analytics summary ----
      const a = d.analytics || {};
      const dur = d.recording.duration_s;
      const pct = (x) => Math.round((x || 0) * 100) + "%";
      const stats = $("railStats");
      if (stats) {
        const cells = [
          ["Viewers", a.unique_viewers ?? 0],
          ["Played", `${a.played ?? 0}/${a.opened ?? 0}`],
          ["Avg watched", pct(a.avg_pct)],
          ["Completed", `${pct(a.completion_rate)}`],
          ["Watch time", fmt(a.total_watch_s || 0)],
          ["Speaking", d.recording.wpm ? `${Math.round(d.recording.wpm)} wpm` : "—"],
        ];
        stats.innerHTML = cells.map(([k, v]) =>
          `<div class="stat"><span class="v num">${v}</span>` +
          `<span class="k">${k}</span></div>`).join("");
      }

      // ---- per-viewer engagement (watched-segments bar) ----
      const vbox = $("railViewers");
      vbox.textContent = "";
      const viewers = d.viewers.filter((v) => !v.is_owner)
        .sort((x, y) => y.watched_s - x.watched_s);
      if (!viewers.length) {
        vbox.innerHTML = '<p class="empty">No views yet — share the link.</p>';
      }
      for (const v of viewers) {
        const row = document.createElement("div");
        row.className = "vrow2";
        const who = v.label || ("viewer " + v.viewer_id.slice(0, 4));
        const reached = dur ? Math.round((v.max_pos_s / dur) * 100) : 0;
        let ranges = [];
        try { ranges = JSON.parse(v.ranges || "[]"); } catch {}
        const segs = dur ? ranges.map(([s, e]) =>
          `<span style="left:${(s / dur) * 100}%;width:${((e - s) / dur) * 100}%"></span>`
        ).join("") : "";
        row.innerHTML = `
          <div class="vhead"><b></b>
            <span class="num vmeta">${fmt(v.watched_s)} · ${reached}%${
              v.play_count > 1 ? " · " + v.play_count + " plays" : ""}</span>
          </div>
          <div class="segbar">${segs}</div>`;
        row.querySelector("b").textContent = who;
        row.title = "Watched the highlighted parts of the video";
        vbox.appendChild(row);
      }
      const cv = $("railDrop");
      const c2 = cv.getContext("2d");
      c2.clearRect(0, 0, cv.width, cv.height);
      const heat = await fetch(`/api/w/${slug}/heatmap`).then((r) => r.json())
        .catch(() => null);
      const buckets = heat?.buckets || new Array(100).fill(0);
      const max = Math.max(1, ...buckets);
      for (let i = 0; i < 100; i++) {
        const h = (buckets[i] / max) * (cv.height - 4);
        c2.fillStyle = "rgba(167, 139, 250, 0.8)";
        c2.fillRect(i * (cv.width / 100), cv.height - h, cv.width / 100 - 1, h);
      }
    };
    refresh();
  };
  buildRail();

  // ---- transcript click-to-seek (word-level if words.json exists) ----
  const tb = $("transcriptBody");
  if (tb && window.DR.hasWords) {
    fetch(`/media/${slug}/words.json`).then((r) => r.json()).then((words) => {
      tb.textContent = "";
      const frag = document.createDocumentFragment();
      words.forEach((w) => {
        const span = document.createElement("span");
        span.className = "tw";
        span.textContent = w.word + " ";
        span.dataset.start = w.start;
        span.addEventListener("click", () => {
          video.currentTime = w.start;
          video.play();
        });
        frag.appendChild(span);
      });
      tb.appendChild(frag);
      video.addEventListener("timeupdate", () => {
        const t = video.currentTime;
        let current = null;
        for (const s of tb.children) {
          const on = parseFloat(s.dataset.start) <= t;
          if (on) current = s;
        }
        tb.querySelector(".now")?.classList.remove("now");
        current?.classList.add("now");
      });
    }).catch(() => {});
  }
})();
