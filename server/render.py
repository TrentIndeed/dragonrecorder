"""Server-side edit rendering.

Cut edits used to be produced by the tray client polling for jobs, which
meant closing the local app left toggles stuck at "render pending". The
server has everything needed — the source mp4 and each detector's cut list
in edits.data — so it does the work itself now and the desktop app is only
a recorder.

One worker thread, one render at a time: this box also serves the player,
and a parallel pile of x264 jobs would starve it.
"""

import json
import logging
import shutil
import subprocess
import threading
from pathlib import Path

import config
import db
from notify import send_telegram

log = logging.getLogger("dr.render")

CUT_KINDS = ("fillers", "silences")
MIN_KEEP_S = 0.12          # drop keep-slivers shorter than this between cuts
IDLE_POLL_S = 300          # backstop; toggles wake the worker immediately

wake = threading.Event()


def ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def available() -> bool:
    return shutil.which("ffmpeg") is not None


def merge_cuts(cuts: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted([c for c in cuts if len(c) == 2 and c[1] > c[0]]):
        if out and s <= out[-1][1] + MIN_KEEP_S:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def keep_segments(cuts: list[list[float]], duration: float) -> list[list[float]]:
    keeps, pos = [], 0.0
    for s, e in merge_cuts(cuts):
        if s - pos >= MIN_KEEP_S:
            keeps.append([pos, s])
        pos = max(pos, e)
    if duration - pos >= MIN_KEEP_S:
        keeps.append([pos, duration])
    return keeps


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            [ffprobe(), "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=60)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        log.warning("could not probe %s", path, exc_info=True)
        return 0.0


def has_audio(path: Path) -> bool:
    try:
        out = subprocess.run(
            [ffprobe(), "-v", "quiet", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60)
        return bool(out.stdout.strip())
    except Exception:
        return False


def render_cuts(video: Path, out: Path, cuts: list[list[float]]) -> None:
    """Drop the cut regions and splice what's left into a new file.

    No NVENC here — the box has no GPU, so libx264 veryfast it is.
    """
    duration = probe_duration(video)
    keeps = keep_segments(cuts, duration)
    if not keeps:
        raise ValueError("cut list removes the entire video")
    expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keeps)
    cmd = [ffmpeg(), "-hide_banner", "-y", "-i", str(video),
           "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB"]
    if has_audio(video):
        cmd += ["-af", f"aselect='{expr}',asetpts=N/SR/TB",
                "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    tmp = out.with_suffix(".part.mp4")
    cmd[-1] = str(tmp)
    r = subprocess.run(cmd, capture_output=True, timeout=3 * 3600)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg failed: "
                           + r.stderr.decode(errors="replace")[-400:])
    # publish atomically: the player must never fetch a half-written file
    tmp.replace(out)


def pending_jobs() -> list[dict]:
    """Enabled cut combos whose derived file does not exist yet."""
    jobs = []
    with db.connect() as dbc:
        rows = dbc.execute(
            "SELECT slug, GROUP_CONCAT(kind) kinds FROM edits"
            " WHERE enabled=1 AND kind IN (?,?) GROUP BY slug",
            CUT_KINDS).fetchall()
        for r in rows:
            kinds = sorted(r["kinds"].split(","))
            name = "cut_" + "+".join(kinds) + ".mp4"
            rec = dbc.execute(
                "SELECT status FROM recordings WHERE slug=?",
                (r["slug"],)).fetchone()
            if not rec or rec["status"] != "ready":
                continue
            if (config.DATA_DIR / r["slug"] / name).exists():
                continue
            cuts = []
            missing = False
            for kind in kinds:
                row = dbc.execute(
                    "SELECT data FROM edits WHERE slug=? AND kind=?",
                    (r["slug"], kind)).fetchone()
                data = json.loads(row["data"]) if row and row["data"] else None
                # the client posts {"cuts": [[s, e], ...]}; tolerate a bare list
                if isinstance(data, dict):
                    data = data.get("cuts")
                if not data:
                    missing = True
                    break
                cuts.extend(data)
            if missing:
                # detector output never arrived (old recording, or the client
                # died mid-pipeline) — nothing to render from
                continue
            jobs.append({"slug": r["slug"], "kinds": kinds, "name": name,
                         "cuts": cuts})
    return jobs


def run_job(job: dict) -> bool:
    slug, name = job["slug"], job["name"]
    src = config.DATA_DIR / slug / "video.mp4"
    if not src.exists():
        log.warning("render %s: source missing", slug)
        return False
    out = config.DATA_DIR / slug / name
    log.info("rendering %s -> %s (%d cuts)", slug, name, len(job["cuts"]))
    try:
        render_cuts(src, out, job["cuts"])
    except Exception as exc:
        log.exception("render failed for %s", slug)
        send_telegram(f"🎬 DragonRecorder: render failed for {slug} — {exc}")
        return False
    with db.connect() as dbc:
        for kind in job["kinds"]:
            dbc.execute("UPDATE edits SET has_render=1 WHERE slug=? AND kind=?",
                        (slug, kind))
    log.info("rendered %s (%.1f MB)", name, out.stat().st_size / 1e6)
    return True


def worker() -> None:
    if not available():
        log.error("ffmpeg not present in the image — cut edits cannot render")
        return
    while True:
        try:
            for job in pending_jobs():
                run_job(job)
        except Exception:
            log.exception("render worker iteration failed")
        wake.wait(timeout=IDLE_POLL_S)
        wake.clear()


def start() -> None:
    threading.Thread(target=worker, name="render-worker", daemon=True).start()
    log.info("render worker started (ffmpeg %s)",
             "available" if available() else "MISSING")
