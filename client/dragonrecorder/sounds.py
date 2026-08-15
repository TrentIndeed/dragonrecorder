"""Audible cues. Deliberately tiny: one non-blocking call, never raises.

The start cue matters because the countdown window is capture-excluded on
some setups and invisible on others — the ding is the one signal that
survives every configuration, including recording a second monitor.
"""

import logging
import threading
from pathlib import Path

log = logging.getLogger("dr.sounds")

ASSETS = Path(__file__).resolve().parent / "assets"


def play(name: str = "start.wav") -> None:
    """Fire and forget. Sound must never delay the start of capture, so the
    call happens on its own thread and swallows anything that goes wrong."""
    path = ASSETS / name
    if not path.exists():
        log.warning("sound %s missing", path)
        return

    def run():
        try:
            import winsound
            # SND_FILENAME so we point at a real file, ASYNC so playback
            # never blocks, NODEFAULT so a missing device stays silent
            # instead of substituting the system beep.
            winsound.PlaySound(
                str(path),
                winsound.SND_FILENAME | winsound.SND_ASYNC
                | winsound.SND_NODEFAULT)
        except Exception:
            log.debug("could not play %s", name, exc_info=True)
    threading.Thread(target=run, daemon=True).start()
