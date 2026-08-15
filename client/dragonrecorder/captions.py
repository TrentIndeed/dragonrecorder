"""Caption cue chunking, modelled on how YouTube renders captions.

Whisper returns segments that can run 15-30 words over 10+ seconds. Rendered
as one cue that is a wall of text sitting on screen long after it was said.
YouTube instead shows a small, fast-moving block:

- at most 2 lines, each around 40 characters
- a cue lasts roughly as long as the words in it take to say, capped ~5s
- a cue is replaced the moment the next one starts, so text never lingers
- breaks land on pauses and sentence ends, not mid-phrase

This module has no heavy imports on purpose: the box re-chunks existing
recordings by loading it straight from the repo checkout.
"""

MAX_LINE = 40          # characters per line before wrapping to line 2
MAX_LINES = 2
MAX_CHARS = MAX_LINE * MAX_LINES
MAX_DUR = 5.0          # a cue never sits on screen longer than this
MIN_DUR = 0.9          # ...nor flashes by faster than this
GAP_BREAK = 0.55       # a pause this long ends the current cue
LINGER = 0.25          # how long a cue may outlive its last word
SENTENCE_END = (".", "!", "?")


def _wrap(text: str) -> str:
    """Balance the cue over at most two lines, breaking on a space."""
    if len(text) <= MAX_LINE:
        return text
    words = text.split()
    best, best_score = None, None
    line = ""
    for i in range(1, len(words)):
        line = " ".join(words[:i])
        rest = " ".join(words[i:])
        if len(line) > MAX_LINE or len(rest) > MAX_LINE:
            continue
        # prefer the split that leaves the two lines closest in length
        score = abs(len(line) - len(rest))
        if best_score is None or score < best_score:
            best, best_score = (line, rest), score
    if best:
        return best[0] + "\n" + best[1]
    # no clean two-line split (one very long word): hard-split at the limit
    return text[:MAX_LINE] + "\n" + text[MAX_LINE:MAX_CHARS]


def chunk_cues(words: list[dict]) -> list[dict]:
    """Group word timings into YouTube-shaped cues."""
    cues: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        text = " ".join(w["word"] for w in cur).strip()
        if text:
            cues.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                         "text": text})
        cur.clear()

    for i, w in enumerate(words):
        if not w.get("word"):
            continue
        # decide BEFORE adding: a cue that only discovers it is too long
        # after the fact still renders one frame too wide, and slow speech
        # (long gaps inside one cue) would blow past the duration cap
        if cur:
            text_len = sum(len(x["word"]) + 1 for x in cur) - 1
            if (text_len + 1 + len(w["word"]) > MAX_CHARS
                    or w["end"] - cur[0]["start"] > MAX_DUR):
                flush()
        cur.append(w)

        text_len = sum(len(x["word"]) + 1 for x in cur) - 1
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0.0
        if gap >= GAP_BREAK or (w["word"].endswith(SENTENCE_END)
                                and text_len >= MAX_LINE * 0.6):
            flush()
    flush()

    # timing pass: give very short cues room to be read, but never let one
    # overlap the next — the next cue replacing it is what keeps captions
    # feeling live rather than lagging behind the speech
    for i, c in enumerate(cues):
        nxt_start = cues[i + 1]["start"] if i + 1 < len(cues) else None
        end = c["end"] + LINGER
        if end - c["start"] < MIN_DUR:
            end = c["start"] + MIN_DUR
        if nxt_start is not None:
            end = min(end, nxt_start)
        c["end"] = max(end, c["start"] + 0.2)
        c["text"] = _wrap(c["text"])
    return cues


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def to_vtt(cues: list[dict]) -> str:
    out = ["WEBVTT", ""]
    for c in cues:
        out.append(f"{_ts(c['start'])} --> {_ts(c['end'])}")
        out.append(c["text"])
        out.append("")
    return "\n".join(out)
