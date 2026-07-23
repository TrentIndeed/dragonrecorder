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


class Recorder:
    """One Recorder per take. Owns the segment list and the live ffmpeg."""

    def __init__(self, take_dir: Path, monitor: int, mic: str, fps: int = 30):
        self.take_dir = take_dir
        self.take_dir.mkdir(parents=True, exist_ok=True)
        self.monitor = monitor
        self.mic = mic
        self.fps = fps
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
            cmd += ["-c:a", "aac", "-b:a", "160k"]
        cmd += ["-movflags", "+faststart", str(out)]
        return cmd

    # -- lifecycle -----------------------------------------------------------

    def start_segment(self) -> None:
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
