/* Record-and-return: when the tray app finishes a take while one of these
   pages is already open, that page takes the recording over and navigates
   itself to it — so your library tab becomes the new video instead of a
   second tab appearing beside it. The app opens a tab of its own only when
   no page claims the take.

   This long-polls the loopback bridge rather than polling on a timer: a
   browser window sitting behind the recorder is "hidden" to Chrome, and
   hidden tabs get their timers throttled to about once a minute — far too
   slow for the handoff. A parked request wakes the moment a take lands.
   On any machine that isn't the recording PC the fetch just fails and we
   settle into a slow retry. */
(() => {
  const BRIDGE = "http://127.0.0.1:8477";
  const here = (window.DR && window.DR.slug) || null;
  // let the library win when both it and a watch page are open
  const CLAIM_DELAY = here ? 350 : 0;
  const RETRY_MS = 20000;
  // only takes announced after this page started listening count
  let since = Date.now() / 1000;
  let stopped = false;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const loop = async () => {
    while (!stopped) {
      let rec = null;
      try {
        const r = await fetch(`${BRIDGE}/wait?since=${since}`,
                              { cache: "no-store" });
        if (!r.ok) throw new Error("bridge");
        rec = await r.json();
      } catch {
        await sleep(RETRY_MS);      // recorder isn't running here
        continue;
      }
      if (rec && rec.at > since) since = rec.at;
      if (!rec || !rec.slug || rec.taken || rec.slug === here) continue;
      try {
        if (CLAIM_DELAY) await sleep(CLAIM_DELAY);
        const c = await fetch(`${BRIDGE}/claim`, { method: "POST" });
        const got = c.ok ? await c.json() : {};
        if (got.first && got.slug) {
          stopped = true;
          location.href = `/w/${encodeURIComponent(got.slug)}`;
        }
      } catch { /* app will open its own tab */ }
    }
  };
  loop();
})();
