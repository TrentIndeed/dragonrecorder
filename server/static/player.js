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
  const speeds = [1, 1.25, 1.5, 1.75, 2];
  let speedIdx = 0;

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
    $("speedBtn").textContent = `${speeds[speedIdx]}×`;
    $("preSpeed").textContent = `${speeds[speedIdx]}×`;
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

  const flush = (beacon = false) => {
    closeRange();
    if (!video.paused) rangeStart = video.currentTime;
    if (!pending.length || window.DR.isOwner) { pending = []; return; }
    const payload = JSON.stringify({ ranges: pending });
    pending = [];
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
