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
import re
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


def warm_up(monitor: int) -> None:
    """Grab a few throwaway frames so the real take starts warm.

    Bringing up D3D11 and desktop duplication takes a few hundred ms the
    first time, and the video clock only starts once it is ready — while
    the mic has been running since the device opened. Measured across
    batches of sync probes, a cold first run reads ~+227 ms against ~+30 ms
    for every run after it. The countdown is dead time anyway, so this
    absorbs the cost where nobody is watching.
    """
    try:
        subprocess.run(
            [config.find_ffmpeg(), "-hide_banner", "-loglevel", "error",
             "-init_hw_device", "d3d11va", "-filter_complex",
             f"ddagrab=output_idx={monitor - 1}:framerate=10",
             "-frames:v", "8", "-f", "null", "-"],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20)
    except Exception:
        log.debug("capture warm-up failed", exc_info=True)


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


def rnnoise_model() -> str | None:
    """Path to the RNNoise weights, escaped for an ffmpeg filter argument."""
    p = Path(__file__).resolve().parent / "assets" / "bd.rnnn"
    if not p.exists():
        return None
    return p.as_posix().replace(":", r"\:")


def _loudness(video: Path, chain: str) -> dict | None:
    """EBU R128 of the audio after `chain`: integrated, range, true peak."""
    r = subprocess.run(
        [config.find_ffmpeg(), "-hide_banner", "-i", str(video), "-af",
         f"{chain},ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        timeout=900)

    def grab(pattern):
        m = re.search(pattern, r.stderr)
        return float(m.group(1)) if m else None

    i = grab(r"I:\s+(-?[\d.]+) LUFS")
    if i is None or not math.isfinite(i) or i < -60:
        return None            # silence: nothing to measure or lift
    return {"I": i, "LRA": grab(r"LRA:\s+(-?[\d.]+) LU") or 0.0,
            "TP": grab(r"Peak:\s+(-?[\d.]+) dBFS") or 0.0}


TARGET_LUFS = -16.0       # what the web expects
WIDE_LRA = 15.0           # above this, even out the range before lifting
QUIET_TAKE_LUFS = -40.0   # below this it is room tone, not speech


def clean_audio(video: Path, denoise: bool = False) -> bool:
    """Clean up and level a finished take, in place.

    Deliberately NOT ffmpeg's loudnorm. Asked to hit a target it runs in
    "Normalization Type: Dynamic" — time-varying gain that flattened a real
    take to 3.9 LU of range and is what made the voice sound processed
    rather than recorded. Measuring first and then applying ONE static gain
    leaves the performance intact; a limiter catches the odd transient.

    Compression is applied only when the take actually needs it (a wide
    range means quiet passages would be inaudible after a flat lift), so a
    well-recorded interface like the SSL 2 passes through untouched apart
    from the gain.

    Runs offline with -c:v copy, so the video is never re-encoded.
    """
    ff = config.find_ffmpeg()
    chain = []
    if audio_channels(video) > 1:
        # takes from before the capture-side fold still carry the voice on
        # one channel only
        chain.append("pan=mono|c0=c0+c1")
    chain.append("highpass=f=80")          # mains hum, desk rumble, HVAC
    if denoise:
        model = rnnoise_model()
        if model:
            # RNNoise, the same class of speech denoiser a browser applies
            # to getUserMedia — which is why Loom sounds cleaner off the
            # same microphone. Measured on a real take: 43.1 -> 54.4 dB SNR,
            # with the 300-3k voice band and the 6-10k air both unchanged.
            # ffmpeg's afftdn managed +0.6 dB on the same file and left
            # musical noise behind.
            chain.append(f"arnndn=m='{model}'")
        else:
            log.warning("no RNNoise model - skipping noise suppression")

    measured = _loudness(video, ",".join(chain))
    if measured is None:
        log.info("take has no measurable speech - leaving the audio alone")
        return True

    if measured["LRA"] > WIDE_LRA:
        chain.append("acompressor=threshold=-22dB:ratio=2.5:attack=15:"
                     "release=250:makeup=3")
        after = _loudness(video, ",".join(chain))
        if after:
            measured = after
        log.info("range was %.1f LU - evening it out before the lift",
                 measured["LRA"])

    gain = TARGET_LUFS - measured["I"]
    # A take that measures this quiet is room tone, not speech: lifting it
    # all the way to target would just make the hiss loud (a silent test
    # take asked for +24 dB). Give it enough to be audible and no more.
    if measured["I"] < QUIET_TAKE_LUFS:
        gain = min(gain, 8.0)
        log.info("take is very quiet (%.1f LUFS) - limiting the lift",
                 measured["I"])
    gain = max(-12.0, min(18.0, gain))
    filters = list(chain)
    if abs(gain) > 0.2:
        filters.append(f"volume={gain:.2f}dB")
    filters.append("alimiter=limit=0.95:level=disabled")
    log.info("audio: %.1f LUFS (range %.1f LU) -> %+.1f dB -> target %.0f "
             "[%s]", measured["I"], measured["LRA"], gain, TARGET_LUFS,
             ", ".join(f.split("=")[0] for f in filters))

    out = video.with_name("clean.mp4")
    r = subprocess.run(
        # dual-mono out: the voice sits centred instead of in one ear, and a
        # stereo file measures at the loudness target players expect (the
        # same audio as mono reads ~3 LU quieter to EBU R128)
        [ff, "-hide_banner", "-y", "-i", str(video), "-c:v", "copy",
         "-af", ",".join(filters),
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
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
                 denoise: bool = True, av_offset_ms: int = 260):
        self.take_dir = take_dir
        self.take_dir.mkdir(parents=True, exist_ok=True)
        self.monitor = monitor
        self.mic = mic
        self.fps = fps
        self.denoise = denoise
        self.av_offset_ms = av_offset_ms
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
            # A/V sync: dshow starts filling its buffer the moment the device
            # opens, while ddagrab needs a few hundred ms to bring up D3D11
            # and desktop duplication — so audio ends up describing an
            # earlier moment than the video beside it. Measured on this
            # machine with a synchronised flash+tone probe (tools/
            # measure_av_sync.py): 689-796 ms of audio lag out of the box.
            # Shrinking the dshow buffer removes ~450 ms of it, and the
            # remaining constant is compensated by itsoffset, leaving a
            # typical residual of about 20 ms.
            cmd += ["-f", "dshow", "-rtbufsize", "64M",
                    # the device offers 48k; without this dshow picks 44.1k,
                    # so Windows resamples down and clean_audio resamples
                    # back up. RNNoise also wants 48k.
                    "-sample_rate", "48000",
                    # 50 ms starved the USB interface: a real take came back
                    # with a 38 ms hole of digital silence in the middle.
                    # 120 ms is still far under the 500 ms default that
                    # caused the original sync problem.
                    "-audio_buffer_size", "120",
                    "-itsoffset", f"-{self.av_offset_ms / 1000:.3f}",
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
            # 256k for the capture pass: this audio is encoded twice (here,
            # then again after cleanup), and a null test put the second
            # generation's artifacts 45 dB under the signal. Bitrate is free
            # on a local file; generation loss is not.
            cmd += ["-af", capture_audio_filter(),
                    "-c:a", "aac", "-b:a", "256k", "-ac", "1"]
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
