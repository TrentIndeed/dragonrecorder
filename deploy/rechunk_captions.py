"""Rebuild captions.vtt for recordings made before the YouTube-style chunker.

Old VTTs came straight from whisper segments: 20+ words held on screen for
10 seconds. Every recording that kept its words.json can be re-cut with no
transcription work — this walks the data dir and rewrites them in place.

Run on the box (the repo checkout is there, so the chunker is imported from
the client package rather than duplicated):

    python3 deploy/rechunk_captions.py /opt/dragonrecorder/data
"""

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHUNKER = REPO / "client" / "dragonrecorder" / "captions.py"


def load_chunker():
    spec = importlib.util.spec_from_file_location("dr_captions", CHUNKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(data_dir: str) -> int:
    captions = load_chunker()
    root = Path(data_dir)
    done = skipped = 0
    for words_file in sorted(root.glob("*/words.json")):
        vtt = words_file.with_name("captions.vtt")
        try:
            words = json.loads(words_file.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  !! {words_file.parent.name}: unreadable words.json ({exc})")
            skipped += 1
            continue
        cues = captions.chunk_cues(words)
        if not cues:
            print(f"  -- {words_file.parent.name}: no cues produced, left alone")
            skipped += 1
            continue
        before = len(vtt.read_text("utf-8").split("-->")) - 1 if vtt.exists() else 0
        vtt.write_text(captions.to_vtt(cues), "utf-8")
        print(f"  ok {words_file.parent.name}: {before} cues -> {len(cues)}")
        done += 1
    print(f"rewrote {done} recording(s), skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/opt/dragonrecorder/data"))
