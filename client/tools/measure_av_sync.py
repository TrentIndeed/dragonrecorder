"""Measure this machine's audio/video capture offset, for av_offset_ms.

Records with the real capture command while firing a white flash and a 1 kHz
tone at the same instant, then finds each in the result. The gap is the sync
error: positive means the audio describes a moment later than the video
beside it (audio lags).

    python client/tools/measure_av_sync.py            # measure current setting
    python client/tools/measure_av_sync.py --runs 4   # average several

Why it is needed: dshow starts buffering the moment the mic opens, while
ddagrab spends a few hundred ms bringing up D3D11 desktop duplication, so
the two streams start describing different moments. Shrinking the dshow
buffer removes most of it and `-itsoffset` compensates the rest; this script
is how that constant was chosen (689-796 ms raw, ~20 ms after).

Note the reading includes the speaker output latency of the tone (tens of
ms), so aim for a small POSITIVE number rather than a perfect zero — audio
arriving slightly late is far less noticeable than audio arriving early.
"""

import argparse
import math
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dragonrecorder import config, devices, recorder  # noqa: E402

FLASH_AT = 4.0
TOTAL = 8.0


def make_tone(path: Path) -> None:
    sr, frames = 44100, bytearray()
    for i in range(int(sr * 0.15)):
        t = i / sr
        env = min(1.0, t / 0.002) * min(1.0, (0.15 - t) / 0.002)
        frames += struct.pack(
            "<h", int(math.sin(2 * math.pi * 1000 * t) * env * 0.9 * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


def one_run(work: Path) -> float | None:
    s = config.load_settings()
    geo = devices.monitor_geometry(s["monitor"])
    tone = work / "tone.wav"
    make_tone(tone)
    out = work / "sync.mp4"
    rec = recorder.Recorder(work, s["monitor"], s["mic"], s.get("fps", 30),
                            denoise=False,
                            av_offset_ms=s.get("av_offset_ms", 260))
    cmd = rec._cmd(out)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=open(work / "ff.log", "wb"),
                            creationflags=0x08000000)
    started = time.monotonic()

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    root.geometry(f"{geo['width']}x{geo['height']}+{geo['left']}+{geo['top']}")

    def fire():
        time.sleep(max(0.0, FLASH_AT - (time.monotonic() - started)))
        import winsound
        winsound.PlaySound(str(tone), winsound.SND_FILENAME | winsound.SND_ASYNC)
        root.configure(bg="white")
        root.update()
        time.sleep(0.12)
        root.configure(bg="black")
        root.update()
        time.sleep(max(0.0, TOTAL - (time.monotonic() - started)))
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(timeout=20)
        root.after(0, root.destroy)

    threading.Thread(target=fire, daemon=True).start()
    root.mainloop()

    ff = config.find_ffmpeg()
    vid = subprocess.run(
        [ff, "-hide_banner", "-i", str(out), "-vf",
         "scale=64:36,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
         "-f", "null", "-"], capture_output=True, text=True,
        creationflags=0x08000000)
    flash = max(
        ((float(m.group(1)), float(m.group(2)))
         for m in re.finditer(r"pts_time:([\d.]+).*?YAVG=([\d.]+)",
                              vid.stderr, re.S)),
        key=lambda p: p[1], default=(None, None))[0]

    aud = subprocess.run(
        [ff, "-hide_banner", "-i", str(out), "-af",
         "bandpass=f=1000:width_type=h:w=80,astats=metadata=1:reset=1,"
         "ametadata=print:key=lavfi.astats.Overall.RMS_level",
         "-f", "null", "-"], capture_output=True, text=True,
        creationflags=0x08000000)
    peaks = [(float(m.group(1)), float(m.group(2))) for m in re.finditer(
        r"pts_time:([\d.]+)\s*\n.*?RMS_level=(-?[\d.]+)", aud.stderr)]
    tone_t = max(peaks, key=lambda p: p[1])[0] if peaks else None
    if flash is None or tone_t is None:
        return None
    return (tone_t - flash) * 1000.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()
    work = config.APPDATA_DIR / "sync-probe"
    work.mkdir(parents=True, exist_ok=True)
    current = config.load_settings().get("av_offset_ms", 260)
    print(f"current av_offset_ms = {current}")
    results = []
    for i in range(args.runs):
        d = one_run(work)
        if d is None:
            print(f"  run {i + 1}: could not find the flash or the tone")
            continue
        results.append(d)
        print(f"  run {i + 1}: {d:+.0f} ms "
              f"({'audio lags' if d > 0 else 'audio leads'})")
    if results:
        mean = sum(results) / len(results)
        print(f"mean {mean:+.0f} ms over {len(results)} run(s)")
        print(f"suggested av_offset_ms = {round(current + mean)}"
              "  (aim for a small positive residual)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
