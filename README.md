# DragonRecorder

Self-hosted Loom. Record your screen and webcam on Windows, get a share link
on your clipboard the instant you stop — before the upload even starts.

1. **Setup once** — pick webcam, monitor, mic, blur. Persisted.
2. **Hotkey** — 3-second countdown, recording starts. Webcam floats as a
   draggable circle.
3. **Hotkey again** — recording stops and **the share link is already on your
   clipboard**. Upload runs in the background; anyone opening the link
   mid-upload sees a processing state that flips live.
4. Whisper transcript, AI title, thumbnail, and one-click edits (remove filler
   words, remove silences, captions) arrive minutes later — non-destructive
   toggles, never silent rewrites.

The recording toolbar and countdown are excluded from capture via
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` — you see them, the video
doesn't. (Loom bakes its toolbar into every recording; we don't.)

<!-- TODO: screenshot of the toolbar mid-record -->
<!-- TODO: demo video — a DragonRecorder link, naturally -->

## Run the client (Windows)

Requires Windows 10 2004+, an NVIDIA GPU (NVENC), Python 3.11+, and an ffmpeg
build with NVENC.

```
cd client
pip install -r requirements.txt
copy ..\.env.example .env   # fill in SERVER_URL and CAPTURE_TOKEN
python -m dragonrecorder
```

## Deploy the server

FastAPI + SQLite behind Caddy on any Linux box. Caddy serves the video bytes
directly (range requests = scrubbing); the app only handles metadata.

```
cd deploy
./deploy.sh
```

See [deploy/](deploy/) for the Caddy site block and systemd unit, and
[docs/decisions.md](docs/decisions.md) for the non-obvious design calls.

## License

MIT
