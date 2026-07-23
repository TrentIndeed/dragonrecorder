"""Cut-and-splice: turn a list of [start, end] regions to REMOVE into a
derived render. Same decision-list idea as dragonEditor's markCutRegion, but
flattened to a single ffmpeg select/aselect pass since we have no live
timeline to keep in sync.

The original is never touched — renders are separate files the server picks
over the original only while the corresponding toggles are on.
"""

import logging
import subprocess
from pathlib import Path

from . import config, recorder

log = logging.getLogger("dr.edits")
CREATE_NO_WINDOW = 0x08000000
MIN_KEEP_S = 0.12   # drop keep-slivers shorter than this between two cuts


def merge_cuts(cuts: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted([c for c in cuts if c[1] > c[0]]):
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


def render_cuts(video: Path, out: Path, cuts: list[list[float]]) -> None:
    duration = recorder.probe_duration(video)
    keeps = keep_segments(cuts, duration)
    if not keeps:
        raise ValueError("cut list removes the entire video")
    expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keeps)
    has_audio = _has_audio(video)
    vf = f"select='{expr}',setpts=N/FRAME_RATE/TB"
    af = f"aselect='{expr}',asetpts=N/SR/TB"
    nvenc = "h264_nvenc" in _encoders()
    cmd = [config.find_ffmpeg(), "-hide_banner", "-y", "-i", str(video),
           "-vf", vf]
    if has_audio:
        cmd += ["-af", af]
    if nvenc:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    cmd += ["-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW,
                       timeout=3600)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError("render failed: "
                           + r.stderr.decode(errors="replace")[-400:])


_enc_cache: str | None = None


def _encoders() -> str:
    global _enc_cache
    if _enc_cache is None:
        _enc_cache = subprocess.run(
            [config.find_ffmpeg(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
            timeout=15).stdout
    return _enc_cache


def _has_audio(video: Path) -> bool:
    r = subprocess.run(
        [config.find_ffprobe(), "-v", "quiet", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        timeout=30)
    return "audio" in r.stdout
