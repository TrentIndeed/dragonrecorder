/* Record-and-return: when the tray app finishes a take while the library
   is open, the library tab takes the recording over and navigates itself to
   it, instead of a second tab appearing beside it. The app opens a tab of
   its own only when no library tab claims the take.

   The library and nothing else. This used to run on watch pages too, which
   meant finishing a take hijacked whichever recording you were watching —
   including the tab the previous take had just opened.

   This long-polls the loopback bridge rather than polling on a timer: a
   browser window sitting behind the recorder is "hidden" to Chrome, and
   hidden tabs get their timers throttled to about once a minute — far too
   slow for the handoff. A parked request wakes the moment a take lands.
   On any machine that isn't the recording PC the fetch just fails and we
   settle into a slow retry. */
(() => {
  const BRIDGE = window.__DR_BRIDGE || "http://127.0.0.1:8477";
  // belt and braces: the template only loads this on the library, and it
  // refuses to run anywhere else
  if (!location.pathname.startsWith("/dash")) return;
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
      if (!rec || !rec.slug || rec.taken) continue;
      try {
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
