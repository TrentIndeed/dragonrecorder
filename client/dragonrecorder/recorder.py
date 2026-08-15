"""ffmpeg capture management.

Primary path: ddagrab (DXGI Desktop Duplication) straight into h264_nvenc —
frames stay on the GPU. Fallback: gdigrab + libx264 for machines without
NVENC. Both paths capture the DWM-composited desktop, so windows marked
WDA_EXCLUDEFROMCAPTURE are invisible to either.

Pause is segment-based: each pause/resume boundary closes one ffmpeg process
and starts another; stop concatenates the segments losslessly.
"""

import json
import logging
import math
import subprocess
import time
from pathlib import Path

from . import config, devices

log = logging.getLogger("dr.recorder")
CREATE_NO_WINDOW = 0x08000000


class FfmpegDied(RuntimeError):
    pass


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            [config.find_ffprobe(), "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
            timeout=30,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _nvenc_available() -> bool:
    try:
        out = subprocess.run(
            [config.find_ffmpeg(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
        return "h264_nvenc" in out.stdout
    except Exception:
        return False


def capture_audio_filter() -> str:
    """The ONLY audio work allowed during live capture: fold to mono.

    Interfaces like the SSL 2 present two input channels and put the mic on
    channel 1, so a straight stereo capture is voice-in-one-ear at half the
    perceived level (measured: ch1 RMS -32 dB, ch2 -84 dB). Summing rather
    than averaging keeps the full level.

    Everything else — hum filter, denoise, loudness — happens after the take
    in clean_audio(). Benchmarked over 8s captures on this machine:

        no filter          30 fps, 0 dropped
        mono fold          30 fps, 0 dropped
        + loudnorm        8.7 fps, 44 dropped, 0.87x realtime

    loudnorm resamples internally to 192 kHz and buffers for lookahead; in
    a live graph that starves the capture and the whole recording judders.
    """
    return "pan=mono|c0=c0+c1"


def audio_channels(path: Path) -> int:
    try:
        out = subprocess.run(
            [config.find_ffprobe(), "-v", "quiet", "-select_streams", "a:0",
             "-show_entries", "stream=channels", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
            timeout=60)
        return int(out.stdout.strip() or 0)
    except Exception:
        return 0


def clean_audio(video: Path, denoise: bool = True) -> bool:
    """Hum filter, denoise and loudness-normalise a finished take in place.

    Runs offline with `-c:v copy`, so the video is never re-encoded and the
    cost is a few seconds of audio processing. Two passes: measure, then
    correct with those measurements, which hits the target properly (a
    single pass undershot by 2.6 LU on a real take).
    """
    ff = config.find_ffmpeg()
    chain = []
    if audio_channels(video) > 1:
        # takes recorded before the capture-side fold (or from a device we
        # did not fold) still carry the voice on one channel only
        chain.append("pan=mono|c0=c0+c1")
    chain.append("highpass=f=80")
    if denoise:
        # FFT denoiser trained on the running noise floor — kills steady
        # hiss/hum without the underwater artifacts of aggressive gates
        chain.append("afftdn=nf=-25:tn=1")
    # speech leveller before the loudness stage: the raw take has a 29 LU
    # range, so gain alone (blocked by near-0 dBFS transients) lifts it
    # barely 1.5 LU and quiet passages stay inaudible
    chain.append("speechnorm=e=6.25:r=0.0001:l=1")
    target = "I=-16:TP=-1.5:LRA=11"

    measured = None     # two-pass measurements, when they are usable
    silent = False      # no speech in the take: nothing to normalise
    try:
        probe = subprocess.run(
            [ff, "-hide_banner", "-i", str(video), "-af",
             ",".join(chain + [f"loudnorm={target}:print_format=json"]),
             "-f", "null", "-"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
            timeout=600)
        blob = probe.stderr[probe.stderr.rfind("{"):probe.stderr.rfind("}") + 1]
        raw = json.loads(blob)
        vals = {k: float(raw[k]) for k in
                ("input_i", "input_tp", "input_lra", "input_thresh",
                 "target_offset")}
        # silence measures -inf, which loudnorm rejects outright ("value -inf
        # out of range") — and there would be nothing to lift anyway
        if all(math.isfinite(v) for v in vals.values()) and vals["input_i"] > -60:
            measured = vals
        else:
            silent = True
            log.info("take has no measurable speech (%s LUFS) — skipping "
                     "loudness normalisation", raw.get("input_i"))
    except Exception:
        log.warning("loudness measurement failed, falling back to one pass",
                    exc_info=True)

    filters = list(chain)
    if measured:
        filters.append(
            f"loudnorm={target}"
            f":measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true")
    elif not silent:
        filters.append(f"loudnorm={target}")
    filters.append("alimiter=limit=0.95")

    out = video.with_name("clean.mp4")
    r = subprocess.run(
        # dual-mono out: the voice sits centred instead of in one ear, and a
        # stereo file measures at the loudness target players expect (the
        # same audio as mono reads ~3 LU quieter to EBU R128)
        [ff, "-hide_banner", "-y", "-i", str(video), "-c:v", "copy",
         "-af", ",".join(filters),
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", str(out)],
        capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=900)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        log.error("audio cleanup failed: %s",
                  r.stderr.decode(errors="replace")[-400:])
        out.unlink(missing_ok=True)
        return False
    out.replace(video)
    return True


class Recorder:
    """One Recorder per take. Owns the segment list and the live ffmpeg."""

    def __init__(self, take_dir: Path, monitor: int, mic: str, fps: int = 30,
                 denoise: bool = True):
        self.take_dir = take_dir
        self.take_dir.mkdir(parents=True, exist_ok=True)
        self.monitor = monitor
        self.mic = mic
        self.fps = fps
        self.denoise = denoise
        self.segments: list[Path] = []
        self.proc: subprocess.Popen | None = None
        self.use_nvenc = _nvenc_available()
        self.recorded_s = 0.0        # duration of closed segments
        self._seg_started = 0.0

    # -- command construction ------------------------------------------------

    def _cmd(self, out: Path) -> list[str]:
        ff = config.find_ffmpeg()
        cmd = [ff, "-hide_banner", "-y"]
        if self.mic:
            cmd += ["-f", "dshow", "-rtbufsize", "64M",
                    "-i", f"audio={self.mic}"]
        if self.use_nvenc:
            cmd += [
                "-init_hw_device", "d3d11va",
                "-filter_complex",
                f"ddagrab=output_idx={self.monitor - 1}:framerate={self.fps}[v]",
                "-map", "[v]",
                *(["-map", "0:a"] if self.mic else []),
                # no -pix_fmt here: ddagrab emits d3d11 GPU frames that go
                # straight into nvenc; forcing a software format breaks it
                "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23",
            ]
        else:
            geo = devices.monitor_geometry(self.monitor)
            cmd += [
                "-f", "gdigrab", "-framerate", str(self.fps),
                "-offset_x", str(geo["left"]), "-offset_y", str(geo["top"]),
                "-video_size", f"{geo['width']}x{geo['height']}",
                "-i", "desktop",
                "-map", f"{1 if self.mic else 0}:v",
                *(["-map", "0:a"] if self.mic else []),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p",
            ]
        if self.mic:
            cmd += ["-af", capture_audio_filter(),
                    "-c:a", "aac", "-b:a", "160k", "-ac", "1"]
        cmd += ["-movflags", "+faststart", str(out)]
        return cmd

    # -- lifecycle -----------------------------------------------------------

    def start_segment(self) -> None:
        # ensure the take dir exists at the moment ffmpeg needs it (a stale
        # cleanup thread or AV could have removed it since __init__)
        self.take_dir.mkdir(parents=True, exist_ok=True)
        out = self.take_dir / f"seg{len(self.segments):02d}.mp4"
        cmd = self._cmd(out)
        log.info("ffmpeg: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=open(self.take_dir / "ffmpeg.log", "ab"),
            creationflags=CREATE_NO_WINDOW,
        )
        time.sleep(0.7)
        if self.proc.poll() is not None:
            if self.use_nvenc:
                log.warning("ddagrab/nvenc path failed, falling back to gdigrab/x264")
                self.use_nvenc = False
                return self.start_segment()
            raise FfmpegDied(self._tail_log())
        self.segments.append(out)
        self._seg_started = time.monotonic()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def elapsed(self) -> float:
        live = time.monotonic() - self._seg_started if self.alive() else 0.0
        return self.recorded_s + live

    def stop_segment(self) -> None:
        """Graceful quit so the moov atom gets written."""
        if not self.proc:
            return
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg ignored q, terminating")
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.proc = None
        if self.segments:
            self.recorded_s += probe_duration(self.segments[-1])

    def finish(self) -> Path:
        """Stop and produce the final faststart mp4."""
        self.stop_segment()
        good = [s for s in self.segments if s.exists() and s.stat().st_size > 0]
        if not good:
            raise FfmpegDied("no video segments were produced: " + self._tail_log())
        final = self.take_dir / "video.mp4"
        if len(good) == 1:
            good[0].rename(final)
        else:
            listfile = self.take_dir / "concat.txt"
            listfile.write_text(
                "".join(f"file '{s.as_posix()}'\n" for s in good), "utf-8")
            r = subprocess.run(
                [config.find_ffmpeg(), "-hide_banner", "-y", "-f", "concat",
                 "-safe", "0", "-i", str(listfile), "-c", "copy",
                 "-movflags", "+faststart", str(final)],
                capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=300,
            )
            if r.returncode != 0 or not final.exists():
                raise FfmpegDied("concat failed: " + r.stderr.decode(errors="replace")[-400:])
        return final

    def abort(self) -> None:
        """Kill without ceremony (trash/restart)."""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    def _tail_log(self) -> str:
        try:
            data = (self.take_dir / "ffmpeg.log").read_bytes()[-500:]
            return data.decode(errors="replace")
        except OSError:
            return "(no ffmpeg log)"
