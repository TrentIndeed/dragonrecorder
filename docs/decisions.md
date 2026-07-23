# Decisions

Non-obvious calls a reader won't reconstruct from the code.

## Instant link: slug minted at record start, not upload completion

The share link hits the clipboard the moment recording stops — before a single
byte uploads. The client mints the slug (and registers a `pending` row on the
server) when recording *starts*. A viewer opening the link mid-upload sees a
"processing" page that polls and flips live when the file lands. This is the
whole product; everything else is polish.

Corollary: a trashed or restarted take must release its slug and delete the
`pending` row, or the reaper and broken-link alerts fire on recordings that
never existed.

## Capture exclusion instead of toolbar-in-video

ffmpeg composites nothing — it grabs the DWM-composited display, so every
visible window lands in the recording. The webcam bubble and drawing overlay
are real windows *meant* to be captured. The toolbar and countdown are real
windows marked `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` (Win10
2004+): visible to the operator, invisible to the capture API. Loom bakes its
toolbar into every recording and has to collapse it mid-record; we exclude it
and keep it full-size.

This only works because our capture path is DXGI Desktop Duplication (via
ffmpeg ddagrab). An older GDI grab would ignore the affinity flag — verified
early, by design (build order step 2).

## All processing client-side, all post-link

Whisper, title generation, thumbnailing, edit detection all run on the Windows
box with the GPU, *after* the link is already on the clipboard. The server
never transcodes, never transcribes. Artifacts are pushed up as each
completes. Keeps the Hetzner box cheap and the clipboard instant.

## Edits are a decision list, never a destructive render

The original file is what's stored and served. Each edit (silences, filler
words, captions) is detected, counted, and exposed as a toggle; toggling
produces a derived render from the original, toggling off restores the
original without re-uploading. Detectors that find nothing render greyed out
with their zero state visible — "0 silences found" proves the detector ran.

## Caddy serves video bytes, not the app

Range requests make scrubbing work. Caddy `file_server` handles the media
directly; FastAPI only handles metadata, analytics, and the pending/expired
states. Proxying video bytes through uvicorn would buy nothing and cost
scrubbing latency.

## Rewind cut from v1

Loom's rewind (back up a few seconds, re-record over the mistake) is real
functionality but is mid-file splice-and-resume. Deliberately out of scope
until everything else ships. Trash + restart cover the common case (flubbed
intro) at a fraction of the complexity.
