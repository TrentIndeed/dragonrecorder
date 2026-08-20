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

  // Loom-style silent preview: the video plays muted behind the play
  // button; pressing Play restarts from the top with sound.
  let previewing = true;
  video.muted = true;
  video.play().catch(() => { previewing = false; });
  const realPlay = () => {
    if (previewing) {
      previewing = false;
      video.pause();
      video.currentTime = 0;
      video.muted = false;
      applyVolume(parseInt($("vol").value, 10));   // honour the saved level
    }
    video.play();
  };

  // Chrome ignores the `hidden` attribute on inline <svg>, so the icon state
  // is driven by an explicit display — never by hidden alone.
  const showIcon = (el, on) => {
    if (!el) return;
    el.hidden = !on;
    el.style.display = on ? "block" : "none";
  };
  const setPlayingUI = (playing) => {
    showIcon($("icoPlay"), !playing);
    showIcon($("icoPause"), playing);
    $("playBtn").setAttribute("aria-label", playing ? "Pause" : "Play");
    // brief center glyph, the way desktop players confirm a pause/resume
    if (!previewing) flashCenter(playing ? null : "pause");
  };

  // center pause indicator: stays up while paused, flashes away on resume
  const pauseflash = $("pauseflash");
  let flashTimer;
  const flashCenter = (kind) => {
    clearTimeout(flashTimer);
    if (kind === "pause") {
      pauseflash.classList.add("show");
    } else {
      pauseflash.classList.remove("show");
    }
  };
  const toggle = () => {
    if (previewing || video.paused) realPlay();
    else video.pause();
  };

  $("bigplay").addEventListener("click", realPlay);
  $("playBtn").addEventListener("click", toggle);
  video.addEventListener("click", toggle);
  video.addEventListener("play", () => {
    if (!previewing) preplay.classList.add("hidden");
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

  // ---- volume, with boost above 100% ----
  // Recordings made before the capture chain was loudness-normalised sit
  // around -23 LUFS, well under the -16 the web expects. video.volume caps
  // at 1, so anything above 100% routes through a WebAudio gain node — set
  // up lazily, because creating it before a user gesture leaves it suspended.
  let audioCtx = null, gainNode = null;
  const ensureGain = () => {
    if (gainNode || !window.AudioContext) return gainNode;
    try {
      audioCtx = new AudioContext();
      const src = audioCtx.createMediaElementSource(video);
      gainNode = audioCtx.createGain();
      src.connect(gainNode).connect(audioCtx.destination);
    } catch (e) {
      audioCtx = gainNode = null;   // fall back to plain video.volume
    }
    return gainNode;
  };

  const volSlider = $("vol");
  const savedVol = parseInt(localStorage.getItem("dr_vol") ?? "100", 10);
  let lastVol = savedVol || 100;
  const applyVolume = (pct) => {
    const v = pct / 100;
    video.muted = previewing ? true : pct === 0;
    if (v > 1) {
      const g = ensureGain();
      if (g) {
        video.volume = 1;
        g.gain.value = v;
        if (audioCtx.state === "suspended") audioCtx.resume();
      } else {
        video.volume = 1;          // no WebAudio: 100% is the ceiling
      }
    } else {
      if (gainNode) gainNode.gain.value = 1;
      video.volume = v;
    }
    volSlider.value = pct;
    volSlider.style.setProperty("--fill", `${(pct / 200) * 100}%`);
    showIcon($("icoVolHigh"), pct >= 50);
    showIcon($("icoVolLow"), pct > 0 && pct < 50);
    showIcon($("icoVolMute"), pct === 0);
    $("volBtn").setAttribute("aria-label", pct === 0 ? "Unmute" : "Mute");
    localStorage.setItem("dr_vol", String(pct));
  };
  volSlider.addEventListener("input", () => {
    const pct = parseInt(volSlider.value, 10);
    if (pct > 0) lastVol = pct;
    applyVolume(pct);
  });
  $("volBtn").addEventListener("click", () => {
    const pct = parseInt(volSlider.value, 10);
    applyVolume(pct === 0 ? lastVol || 100 : 0);
  });
  applyVolume(savedVol);

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

    // Lift cues clear of the control bar, as YouTube does — otherwise the
    // caption sits under the scrubber on small players.
    const liftCues = () => {
      if (!track || !track.cues) return;
      for (const cue of track.cues) {
        cue.snapToLines = true;
        cue.line = -3;
      }
    };
    track?.addEventListener("load", liftCues);
    video.addEventListener("loadeddata", liftCues);
    liftCues();
  }

  // Caption size follows the *player* width, not the viewport: the same
  // clamp that reads well on a 1600px stage is enormous on a phone.
  const sizeCues = () => {
    const w = video.clientWidth || 640;
    const px = Math.max(13, Math.min(30, Math.round(w * 0.021)));
    document.documentElement.style.setProperty("--cue-size", `${px}px`);
  };
  new ResizeObserver(sizeCues).observe(video);
  sizeCues();

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
    if (e.key === "m") $("volBtn").click();
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      const cur = parseInt($("vol").value, 10);
      applyVolume(Math.max(0, Math.min(200, cur + (e.key === "ArrowUp" ? 10 : -10))));
    }
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
  video.addEventListener("play", () => {
    if (!previewing) rangeStart = video.currentTime;
  });
  video.addEventListener("pause", closeRange);
  video.addEventListener("seeking", () => { closeRange(); });
  video.addEventListener("seeked", () => { if (!video.paused) rangeStart = video.currentTime; });
  video.addEventListener("ended", closeRange);

  // play-count + time-to-first-play (engagement signal for the owner)
  let pendingPlays = 0;
  let firstPlayS = null;
  const pageOpen = performance.now();
  video.addEventListener("play", () => {
    if (previewing) return;
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

  const ptoast = (msg) => {
    const t = $("ptoast");
    t.textContent = msg;
    t.classList.add("show");
    setTimeout(() => t.classList.remove("show"), 1800);
  };


  // ---- live metadata: fill the page in as processing lands ----
  // A recording is watchable seconds after it stops, but its title,
  // description, captions and chapters arrive up to a minute later. Poll for
  // them so an open tab updates itself instead of needing a refresh.
  let activeChapters = [];
  const renderChapters = (chapters) => {
    if (!Array.isArray(chapters) || chapters.length < 2) return;
    const list = $("chapterList");
    if (list.dataset.filled === String(chapters.length)) return;
    list.dataset.filled = String(chapters.length);
    activeChapters = chapters;
    list.textContent = "";
    $("chapters").hidden = false;
    for (const c of chapters) {
      const b = document.createElement("button");
      b.className = "chaprow";
      b.innerHTML = `<span class="t num">${fmt(c.t)}</span><span></span>`;
      b.querySelector("span:last-child").textContent = c.title;
      b.addEventListener("click", () => { video.currentTime = c.t; realPlay(); });
      list.appendChild(b);
    }
    const dur0 = window.DR.duration;
    scrub.querySelectorAll(".chapgap").forEach((g) => g.remove());
    if (dur0) {
      for (const c of chapters.slice(1)) {
        const gap = document.createElement("div");
        gap.className = "chapgap";
        gap.style.left = `${(c.t / dur0) * 100}%`;
        scrub.appendChild(gap);
      }
    }
  };

  const applyMeta = (s) => {
    if (s.title) {
      const h1 = $("title");
      const pill = h1.querySelector(".pill");
      if (h1.childNodes[0]?.nodeValue?.trim() !== s.title) {
        h1.childNodes[0].nodeValue = s.title;
        document.title = `${s.title} — DragonRecorder`;
      }
      if (s.title_is_ai && !pill) {
        const p = document.createElement("span");
        p.className = "pill ai-badge";
        p.title = "Title generated from the transcript";
        p.textContent = "AI";
        h1.appendChild(p);
      }
    }
    if (s.description) {
      let desc = document.querySelector(".desc");
      if (!desc) {
        desc = document.createElement("div");
        desc.className = "desc";
        desc.innerHTML = '<p></p><div class="aihint">' +
          "Description generated from the transcript</div>";
        $("stage").after(desc);
      }
      const p = desc.querySelector("p");
      if (p.textContent !== s.description) p.textContent = s.description;
    }
    if (s.has_vtt && !video.querySelector("track")) {
      const t = document.createElement("track");
      t.kind = "captions";
      t.label = "Captions";
      t.srclang = "en";
      t.src = `/media/${slug}/captions.vtt`;
      t.default = true;
      video.appendChild(t);
      if (video.textTracks[0]) video.textTracks[0].mode = "showing";
    }
    renderChapters(s.chapters);
    if (s.has_transcript) ensureTranscript(s.has_words);
    // an edit finished rendering (or was switched off): move to the file the
    // server says should be playing, keeping the viewer's place
    if (s.video_file && s.video_file !== currentFile) {
      swapVideo(s.video_file, s.cuts || []);
    } else if (Array.isArray(s.cuts) && s.cuts.length !== cutRegions.length) {
      cutRegions = s.cuts;
      currentCuts = s.cuts;
      if (cutRegions.length) loadPeaks().then(drawWave);
      else drawWave();
    }
    const pill = $("viewPill");
    if (pill && typeof s.views === "number") {
      pill.textContent = `${s.views} view${s.views === 1 ? "" : "s"}`;
    }
  };

  const pollMeta = async () => {
    let next = 5000;      // brisk while the pipeline is still landing
    try {
      if (!document.hidden) {
        const r = await fetch(`/api/w/${slug}/state`);
        if (r.ok) {
          const s = await r.json();
          applyMeta(s);
          // keep watching after processing finishes, just less often, so a
          // rename or a re-run also shows up on an already-open tab
          if (s.analyzed && s.title) next = 15000;
        }
      }
    } catch (e) {}
    setTimeout(pollMeta, next);
  };
  setTimeout(pollMeta, 3000);
  // check straight away when the tab comes back to the front
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollMeta();
  });

  // chapters that were already in the page, plus the current-chapter tracker
  // (renderChapters handles both these and any that arrive from the poll)
  renderChapters(window.DR.chapters);
  video.addEventListener("timeupdate", () => {
    if (!activeChapters.length) return;
    const t = video.currentTime;
    let cur = activeChapters[0], idx = 0;
    activeChapters.forEach((c, i) => { if (c.t <= t) { cur = c; idx = i; } });
    $("chapName").textContent = "· " + cur.title;
    $("chapterList").querySelectorAll(".chaprow").forEach((r, i) =>
      r.classList.toggle("now", i === idx));
  });

  // ---- waveform strip: shows what the enabled cuts take out ----
  // Peaks are of the ORIGINAL take, so the shaded regions line up with the
  // cut list even though the video playing is already the edited render.
  let peaksData = null;
  let cutRegions = [];

  const drawWave = () => {
    const strip = $("waveStrip");
    if (!peaksData || !cutRegions.length) { strip.hidden = true; return; }
    strip.hidden = false;
    const cv = $("waveCanvas");
    const cssW = cv.clientWidth || 600;
    const cssH = cv.clientHeight || 56;
    cv.width = cssW * devicePixelRatio;
    cv.height = cssH * devicePixelRatio;
    const g = cv.getContext("2d");
    g.scale(devicePixelRatio, devicePixelRatio);
    g.clearRect(0, 0, cssW, cssH);
    const peaks = peaksData.peaks;
    const dur = peaksData.duration || window.DR.duration || 1;
    const mid = cssH / 2;
    const barW = Math.max(1, cssW / peaks.length);
    const accent = getComputedStyle(document.documentElement)
      .getPropertyValue("--accent").trim() || "#2c6bff";
    const isCut = (t) => cutRegions.some(([s, e]) => t >= s && t <= e);
    // Normalise to the loudest peak and bend the curve: speech sits far
    // below full scale, so a linear strip is a flat line with a few spikes.
    const loudest = Math.max(0.02, ...peaks);
    for (let i = 0; i < peaks.length; i++) {
      const t = (i / peaks.length) * dur;
      const rel = Math.pow(peaks[i] / loudest, 0.55);
      const h = Math.max(1.5, rel * (cssH - 6));
      g.fillStyle = isCut(t) ? "#9aa3b2" : accent;
      g.fillRect(i * barW, mid - h / 2, Math.max(1, barW - 0.5), h);
    }
    const removed = cutRegions.reduce((a, [s, e]) => a + (e - s), 0);
    $("waveNote").textContent =
      `${fmt(removed)} removed of ${fmt(dur)}`;
    movePlayhead();
  };
  new ResizeObserver(drawWave).observe($("waveStrip"));

  // The edited render's clock skips the removed stretches, so moving
  // between the two timelines means walking the kept segments. Both
  // directions take an explicit cut list: the page may be swapping from one
  // edit to another, and each side needs its own.
  const keptSegments = (cuts) => {
    const dur = peaksData?.duration || window.DR.duration || 0;
    const keeps = [];
    let pos = 0;
    for (const [s, e] of [...(cuts || [])].sort((a, b) => a[0] - b[0])) {
      if (s > pos) keeps.push([pos, s]);
      pos = Math.max(pos, e);
    }
    if (dur > pos) keeps.push([pos, dur]);
    return keeps;
  };
  const editedToOriginal = (t, cuts = cutRegions) => {
    let left = t;
    for (const [s, e] of keptSegments(cuts)) {
      const span = e - s;
      if (left <= span) return s + left;
      left -= span;
    }
    return peaksData?.duration || t;
  };
  const originalToEdited = (t, cuts = cutRegions) => {
    let acc = 0;
    for (const [s, e] of keptSegments(cuts)) {
      if (t < s) return acc;
      if (t <= e) return acc + (t - s);
      acc += e - s;
    }
    return acc;
  };

  // ---- swap to a newly rendered edit without interrupting the viewer ----
  // Reloading the page was jarring: it lost the play position, the volume
  // handling and the scroll. This swaps the source in place and lands on
  // the same moment of speech, because the position is mapped through the
  // cut lists rather than kept as a raw number.
  let currentFile = window.DR.videoFile || "video.mp4";
  let currentCuts = [];
  const swapVideo = (file, newCuts) => {
    if (file === currentFile) { currentCuts = newCuts; return; }
    const wasPlaying = !video.paused && !previewing;
    const from = currentFile, fromCuts = currentCuts;
    let target = video.currentTime;
    if (from === "video.mp4" && file !== "video.mp4") {
      target = originalToEdited(target, newCuts);
    } else if (from !== "video.mp4" && file === "video.mp4") {
      target = editedToOriginal(target, fromCuts);
    } else {
      target = originalToEdited(editedToOriginal(target, fromCuts), newCuts);
    }
    currentFile = file;
    currentCuts = newCuts;
    cutRegions = newCuts;
    video.addEventListener("loadedmetadata", () => {
      const dur = video.duration || 0;
      video.currentTime = Math.max(0, Math.min(target, Math.max(0, dur - 0.05)));
      if (wasPlaying) video.play().catch(() => {});
      drawWave();
    }, { once: true });
    video.src = `/media/${slug}/${file}`;
    video.load();
    ptoast(file === "video.mp4" ? "Back to the original"
                                : "Playing your edited version");
  };

  const movePlayhead = () => {
    const head = $("wavePlayhead");
    if (!peaksData || !cutRegions.length) return;
    const dur = peaksData.duration || 1;
    const shown = cutRegions.length ? editedToOriginal(video.currentTime)
                                    : video.currentTime;
    head.style.left = `${Math.min(100, (shown / dur) * 100)}%`;
  };
  video.addEventListener("timeupdate", movePlayhead);

  // clicking the strip seeks, using the same mapping in reverse
  $("waveStrip").addEventListener("click", (e) => {
    if (!peaksData) return;
    const wrap = document.querySelector(".wavewrap");
    const rect = wrap.getBoundingClientRect();
    if (e.clientX < rect.left || e.clientX > rect.right) return;
    const frac = (e.clientX - rect.left) / rect.width;
    const original = frac * (peaksData.duration || 0);
    video.currentTime = cutRegions.length ? originalToEdited(original) : original;
  });

  const loadPeaks = async () => {
    if (peaksData) return peaksData;
    try {
      const r = await fetch(`/media/${slug}/peaks.json`);
      if (r.ok) peaksData = await r.json();
    } catch (e) {}
    return peaksData;
  };

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

    // Take action buttons
    $("actRecord")?.addEventListener("click", async () => {
      try {
        const r = await fetch("http://127.0.0.1:8477/open", { method: "POST" });
        if (r.ok) return ptoast("Recorder opened — check the top-right panel");
        throw new Error();
      } catch {
        ptoast("Recorder isn't running on this machine");
      }
    });
    $("actCopyLink")?.addEventListener("click", async () => {
      await navigator.clipboard.writeText(location.origin + "/w/" + slug);
      ptoast("Link copied");
    });
    $("actCopyTranscript")?.addEventListener("click", async () => {
      await navigator.clipboard.writeText(window.DR.transcript || "");
      ptoast("Transcript copied");
    });
    $("actEditLink")?.addEventListener("click", async () => {
      const cur = slug;
      const next = prompt("Custom link name (3-60 letters, digits, - or _):", cur);
      if (!next || next === cur) return;
      const r = await fetch(`/api/dash/recordings/${slug}/slug`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug: next }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        await navigator.clipboard.writeText(location.origin + "/w/" + d.slug);
        location.href = "/w/" + d.slug;
      } else {
        ptoast(d.detail || "Couldn't change the link");
      }
    });

    // move the transcript into its rail tab
    const ts = document.getElementById("transcriptSection");
    if (ts) {
      document.getElementById("pane-transcript").appendChild(ts);
      ts.classList.add("in-rail");
    } else {
      document.getElementById("pane-transcript").innerHTML =
        '<p class="empty">No transcript for this recording.</p>';
    }

    let analyzeTimer = null;
    const refresh = async () => {
      const d = await (await fetch(`/api/dash/recordings/${slug}`)).json();
      const box = $("railEdits");
      box.textContent = "";
      const byKind = Object.fromEntries(d.edits.map((e) => [e.kind, e]));
      // no edit rows at all = the recorder has not finished analysing yet.
      // Without saying so, a fresh recording looks identical to one where
      // the detectors ran and found nothing.
      if (!d.edits.length) {
        box.innerHTML =
          '<div class="edit-row zero"><span>Analyzing the recording —' +
          ' transcript, title and edits appear here shortly.</span></div>';
        clearTimeout(analyzeTimer);
        analyzeTimer = setTimeout(refresh, 5000);
        return;
      }
      for (const kind of ["fillers", "silences", "captions"]) {
        const e = byKind[kind];
        const [label, unit] = EDIT_LABELS[kind];
        const row = document.createElement("label");
        row.className = "edit-row" + (!e || !e.count ? " zero" : "");
        // distinguish "we looked and there were none" from "not analyzed"
        if (e && !e.count) row.title = "Analyzed — nothing of this kind found";
        const pendingNote = e && e.count && kind !== "captions" && e.enabled && !e.has_render
          ? '<span class="note">render pending — the recorder picks this up</span>' : "";
        row.innerHTML = `
          <input type="checkbox" ${e?.enabled ? "checked" : ""}
                 ${!e || !e.count ? "disabled" : ""}>
          <span>${label}${pendingNote}</span>
          <span class="cnt num">${e == null ? "not analyzed"
            : (e.count ? `${e.count} found` : "none found")}</span>`;
        row.querySelector("input").addEventListener("change", async (ev) => {
          // The box was unreachable once and this silently did nothing: the
          // checkbox stayed ticked while the server still had the edit off,
          // so it looked like the edit "did not apply". Put the tick back
          // and say so if the request does not land.
          const wanted = ev.target.checked;
          try {
            const res = await fetch(
              `/api/dash/recordings/${slug}/edits/${kind}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: wanted }),
              });
            if (!res.ok) throw new Error(res.status);
          } catch (err) {
            ev.target.checked = !wanted;
            ptoast("Couldn't save that — the server didn't respond");
            return;
          }
          // poke the local recorder to render right now (works when this
          // browser runs on the recording machine; harmless elsewhere)
          fetch("http://127.0.0.1:8477/render", { method: "POST" })
            .catch(() => {});
          watchRender();
          refresh();
        });
        box.appendChild(row);
      }

      // waveform of what the enabled cuts remove
      cutRegions = [];
      for (const kind of ["fillers", "silences"]) {
        const e = byKind[kind];
        if (!e || !e.enabled || !e.data) continue;
        try {
          const parsed = JSON.parse(e.data);
          const list = Array.isArray(parsed) ? parsed : parsed.cuts || [];
          cutRegions.push(...list);
        } catch (err) {}
      }
      if (cutRegions.length) await loadPeaks();
      drawWave();

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
      window.__railRefresh = refresh;
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

  // after toggling a cut edit, poll until its render lands, then reload the
  // video so the applied cut is what plays
  let renderWatch = null;
  function watchRender() {
    if (renderWatch) return;
    let tries = 0;
    renderWatch = setInterval(async () => {
      tries += 1;
      const d = await fetch(`/api/dash/recordings/${slug}`)
        .then((r) => r.json()).catch(() => null);
      if (!d) return;
      const pendingCut = d.edits.some((e) =>
        ["fillers", "silences"].includes(e.kind) && e.enabled && !e.has_render);
      if (window.__railRefresh) window.__railRefresh();
      if (!pendingCut || tries > 60) {
        clearInterval(renderWatch);
        renderWatch = null;
        // no reload: pollMeta sees the new file and swaps it in underneath
        // the viewer, keeping their position and whether they were playing
        if (!pendingCut) pollMeta();
      }
    }, 4000);
  }

  // ---- transcript: build it whenever it lands, not only at page load ----
  // The recorder opens this page the moment the take uploads, so the
  // transcript usually does not exist yet when the HTML is rendered. It
  // used to stay missing forever (and the owner rail said "No transcript
  // for this recording" permanently); now the poll fills it in.
  const wireWords = (tb) => {
    if (!window.DR.hasWords || tb.dataset.wired) return;
    tb.dataset.wired = "1";
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
        for (const el of tb.children) {
          if (parseFloat(el.dataset.start) <= t) current = el;
        }
        tb.querySelector(".now")?.classList.remove("now");
        current?.classList.add("now");
      });
    }).catch(() => {});
  };

  const ensureTranscript = async (hasWords) => {
    if (hasWords) window.DR.hasWords = true;
    let section = document.getElementById("transcriptSection");
    if (section && section.dataset.filled) return;
    const res = await fetch(`/api/w/${slug}/transcript`).catch(() => null);
    const text = res && res.ok ? (await res.json()).text : "";
    if (!text) return;
    window.DR.transcript = text;          // so "Copy transcript" is current
    if (!section) {
      section = document.createElement("section");
      section.className = "transcript";
      section.id = "transcriptSection";
      section.setAttribute("aria-label", "Transcript");
      section.innerHTML =
        '<div class="thead"><h2>Transcript</h2>' +
        '<button class="copytx" type="button">Copy</button></div>' +
        '<div id="transcriptBody"></div>';
      const pane = document.getElementById("pane-transcript");
      if (document.body.classList.contains("owner") && pane) {
        pane.textContent = "";            // drop the "No transcript" placeholder
        pane.appendChild(section);
        section.classList.add("in-rail");
      } else {
        document.querySelector(".cols")?.appendChild(section);
      }
    }
    const tb = section.querySelector("#transcriptBody");
    tb.textContent = text;
    section.dataset.filled = "1";
    wireWords(tb);
  };

  // delegated: the transcript section may be built later by the poll
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".copytx");
    if (!btn) return;
    const text = window.DR.transcript
      || document.getElementById("transcriptBody")?.innerText || "";
    if (!text.trim()) return ptoast("No transcript yet");
    try {
      await navigator.clipboard.writeText(text.trim());
      ptoast("Transcript copied");
    } catch (err) {
      ptoast("Couldn't copy — check clipboard permissions");
    }
  });

  const tb0 = $("transcriptBody");
  if (tb0) {
    document.getElementById("transcriptSection").dataset.filled = "1";
    wireWords(tb0);
  }

})();
