"""Post-link processing. Nothing here may delay the clipboard — it all runs
after upload, pushing artifacts to the server as each completes.

Order: transcript first (everything depends on it) → AI title/description →
thumbnail → edit detection (fillers, silences, captions) → derived renders
for whatever auto-apply enables.

Transcription and silence/filler detection are ported from dragonEditor
(transcribe-server.py): faster-whisper with word timestamps + VAD, silences
inferred from inter-segment gaps, fillers matched against a word list.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import api, captions, config, recorder
from .edits_render import _has_audio

log = logging.getLogger("dr.processing")
CREATE_NO_WINDOW = 0x08000000

FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhm", "ah", "ahh", "er", "erm",
           "hmm", "hm", "mhm", "mm", "like"}
FILLER_BIGRAMS = {("you", "know"), ("i", "mean"), ("sort", "of"), ("kind", "of")}
MIN_SILENCE_S = 1.0
# Two different jobs, so two constants. For a silence the pad SHRINKS the
# cut, and it has to be generous: whisper's segment ends land early (its VAD
# trims the decay of the last word), so a 50 ms margin was slicing the tail
# off whatever was said before the pause. For a filler the pad GROWS the
# cut, so it stays tight or it eats the neighbouring words.
SILENCE_PAD_S = 0.25
FILLER_PAD_S = 0.05
MIN_CUT_S = 0.30          # not worth a splice below this

_model = None
_model_cpu_only = False


def _whisper(force_cpu: bool = False):
    global _model, _model_cpu_only
    if force_cpu and not _model_cpu_only:
        _model = None
        _model_cpu_only = True
    if _model is None:
        from faster_whisper import WhisperModel
        if not _model_cpu_only:
            try:
                _model = WhisperModel(config.WHISPER_MODEL, device="cuda",
                                      compute_type="float16")
                return _model
            except Exception:
                log.info("no CUDA for whisper, using CPU int8")
                _model_cpu_only = True
        _model = WhisperModel(config.WHISPER_MODEL, device="cpu",
                              compute_type="int8")
    return _model


def extract_audio(video: Path) -> Path:
    wav = video.with_name("audio.wav")
    subprocess.run(
        [config.find_ffmpeg(), "-hide_banner", "-y", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", str(wav)],
        capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=600,
        check=True)
    return wav


def transcribe(wav: Path) -> dict:
    try:
        return _transcribe_with(_whisper(), wav)
    except RuntimeError as exc:
        # CUDA builds can fail lazily (missing cuBLAS/cuDNN DLLs) — the
        # constructor succeeds and the first encode blows up. Retry on CPU.
        log.warning("whisper GPU run failed (%s), retrying on CPU", exc)
        return _transcribe_with(_whisper(force_cpu=True), wav)


def _transcribe_with(model, wav: Path) -> dict:
    segments_iter, info = model.transcribe(
        str(wav), beam_size=5, language="en", vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 800},
        word_timestamps=True)
    segments, words = [], []
    for seg in segments_iter:
        segments.append({"start": seg.start, "end": seg.end,
                         "text": seg.text.strip()})
        for w in seg.words or []:
            words.append({"word": w.word.strip(), "start": w.start,
                          "end": w.end})
    return {"segments": segments, "words": words,
            "duration": info.duration,
            "text": " ".join(s["text"] for s in segments)}


def detect_silences(segments: list[dict], duration: float,
                    words: list[dict] | None = None) -> list[list[float]]:
    """Gap-based, as in dragonEditor: inter-segment gaps over threshold.

    Word timings bound the cut when they are available — they are tighter
    than segment boundaries, so less speech is at risk of being clipped.
    """
    cuts = []
    if not segments:
        return cuts

    def last_word_end_before(t: float) -> float:
        """End of the last word at or before t, for a safer cut-in point."""
        if not words:
            return t
        ends = [w["end"] for w in words if w["end"] <= t + 0.001]
        return max(ends) if ends else t

    def first_word_start_after(t: float) -> float:
        if not words:
            return t
        starts = [w["start"] for w in words if w["start"] >= t - 0.001]
        return min(starts) if starts else t

    def add(start: float, end: float) -> None:
        if end - start >= MIN_CUT_S:
            cuts.append([round(max(0.0, start), 3), round(end, 3)])

    if segments[0]["start"] > MIN_SILENCE_S:
        add(0.0, first_word_start_after(segments[0]["start"]) - SILENCE_PAD_S)
    for a, b in zip(segments, segments[1:]):
        if b["start"] - a["end"] > MIN_SILENCE_S:
            add(last_word_end_before(a["end"]) + SILENCE_PAD_S,
                first_word_start_after(b["start"]) - SILENCE_PAD_S)
    if duration and duration - segments[-1]["end"] > MIN_SILENCE_S:
        add(last_word_end_before(segments[-1]["end"]) + SILENCE_PAD_S, duration)
    return cuts


QUIET_RATIO = 0.06        # share of the loudest peak that still counts as quiet


def quiet_regions(peaks_data: dict) -> list[list[float]]:
    """Stretches that are actually quiet, measured from the audio itself."""
    peaks = peaks_data.get("peaks") or []
    dur = peaks_data.get("duration") or 0
    if not peaks or not dur:
        return []
    floor = max(peaks) * QUIET_RATIO
    step = dur / len(peaks)
    out, run_start = [], None
    for i, p in enumerate(peaks):
        if p <= floor:
            if run_start is None:
                run_start = i * step
        elif run_start is not None:
            out.append([run_start, i * step])
            run_start = None
    if run_start is not None:
        out.append([run_start, dur])
    return out


def intersect(a: list[list[float]], b: list[list[float]],
              min_len: float = MIN_CUT_S) -> list[list[float]]:
    """Overlap of two region lists (both assumed sorted, non-overlapping)."""
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if e - s >= min_len:
            out.append([round(s, 3), round(e, 3)])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def refine_silences(cuts: list[list[float]],
                    peaks_data: dict | None) -> list[list[float]]:
    """Only cut where the transcript AND the waveform agree it is silent.

    Whisper's VAD drops anything it does not read as speech, so a stretch of
    real audio it skipped — a cough, a keyboard, a sentence it missed — looks
    like a gap and was being cut out. Intersecting with measured quiet keeps
    audible content in the video.
    """
    if not peaks_data or not cuts:
        return cuts
    quiet = quiet_regions(peaks_data)
    if not quiet:
        return cuts
    # a refined piece still has to be a real pause, or removing it just
    # chops half-second holes between bursts of audio
    refined = intersect(cuts, quiet, min_len=MIN_SILENCE_S)
    dropped = sum(e - s for s, e in cuts) - sum(e - s for s, e in refined)
    if dropped > 0.05:
        log.info("silence cuts trimmed by %.1fs — that audio was not silent",
                 dropped)
    return refined


def detect_fillers(words: list[dict]) -> list[list[float]]:
    """Word-level filler cuts from whisper word timestamps."""
    clean = [re.sub(r"[^a-z']", "", w["word"].lower()) for w in words]
    cuts, i = [], 0
    while i < len(clean):
        if i + 1 < len(clean) and (clean[i], clean[i + 1]) in FILLER_BIGRAMS:
            cuts.append([max(0, words[i]["start"] - FILLER_PAD_S),
                         words[i + 1]["end"] + FILLER_PAD_S])
            i += 2
            continue
        if clean[i] in FILLERS:
            cuts.append([max(0, words[i]["start"] - FILLER_PAD_S),
                         words[i]["end"] + FILLER_PAD_S])
        i += 1
    return cuts


TARGET_WPM = 165          # brisk, energetic sales-pitch delivery
SPEED_STEPS = [1.0, 1.1, 1.2, 1.25, 1.3, 1.4, 1.5]


def measure_wpm(words: list[dict]) -> float:
    """Words per minute of actual speech (excludes gaps > 2s so pauses don't
    deflate the rate). Word-level whisper timestamps make this exact."""
    if len(words) < 5:
        return 0.0
    speech_s = 0.0
    for w in words:
        dur = w["end"] - w["start"]
        if 0 < dur < 2.0:
            speech_s += dur
    # bridge tiny inter-word gaps so the rate reflects delivery, not silence
    for a, b in zip(words, words[1:]):
        gap = b["start"] - a["end"]
        if 0 < gap < 0.4:
            speech_s += gap
    return (len(words) / speech_s * 60.0) if speech_s > 1 else 0.0


def best_speed(wpm: float) -> float:
    """Playback speed that brings a slow talker up to an energetic pace for
    a sales pitch. Never slows anyone down; caps at 1.5x."""
    if wpm <= 0:
        return 1.0
    raw = TARGET_WPM / wpm
    return min(SPEED_STEPS, key=lambda s: abs(s - raw))


def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def write_vtt(segments: list[dict], out: Path,
              words: list[dict] | None = None) -> int:
    """YouTube-shaped cues when word timings exist, whisper segments if not.

    Whisper's segments are far too long to read as captions (one 12-second
    block of 25 words); captions.chunk_cues re-cuts them into the short,
    fast-replacing blocks YouTube uses.
    """
    if words:
        cues = captions.chunk_cues(words)
        if cues:
            out.write_text(captions.to_vtt(cues), "utf-8")
            return len(cues)
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{_ts(seg['start'])} --> {_ts(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    out.write_text("\n".join(lines), "utf-8")
    return len(segments)


PEAK_BUCKETS = 1200       # enough detail for a strip a few hundred px wide


def make_peaks(video: Path, duration: float) -> Path | None:
    """Waveform summary of the take, for the cut-preview strip on the page.

    Decodes to low-rate mono PCM and keeps the loudest sample per bucket —
    peaks, not averages, so short words still show up as bumps.
    """
    if not duration:
        return None
    out = video.with_name("peaks.json")
    r = subprocess.run(
        [config.find_ffmpeg(), "-hide_banner", "-v", "quiet", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=600)
    if r.returncode != 0 or not r.stdout:
        log.warning("peaks: could not decode audio")
        return None
    import array
    samples = array.array("h")
    samples.frombytes(r.stdout[:len(r.stdout) // 2 * 2])
    if not samples:
        return None
    per = max(1, len(samples) // PEAK_BUCKETS)
    peaks = []
    for i in range(0, len(samples), per):
        chunk = samples[i:i + per]
        peaks.append(round(max(abs(min(chunk)), abs(max(chunk))) / 32768, 3))
    out.write_text(json.dumps({"duration": round(duration, 3),
                               "peaks": peaks[:PEAK_BUCKETS]}), "utf-8")
    return out


def make_thumbnail(video: Path, duration: float) -> Path | None:
    thumb = video.with_name("thumb.jpg")
    at = max(0.0, min(duration * 0.2, duration - 0.5)) if duration else 0.0
    r = subprocess.run(
        [config.find_ffmpeg(), "-hide_banner", "-y", "-ss", f"{at:.2f}",
         "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-2",
         "-q:v", "4", str(thumb)],
        capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=120)
    return thumb if r.returncode == 0 and thumb.exists() else None


def _find_claude() -> list[str] | None:
    """The claude CLI may be an npm .cmd shim, which subprocess can only run
    through cmd.exe."""
    found = shutil.which("claude")
    if not found:
        for cand in (Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
                     Path.home() / ".local" / "bin" / "claude.exe"):
            if cand.exists():
                found = str(cand)
                break
    if not found:
        return None
    if found.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", found]
    return [found]


def ai_title(transcript: str) -> dict | None:
    """Claude Code CLI, same pattern as dragonEditor — uses the local
    subscription, no API key. Falls back to None (heuristic title)."""
    claude = _find_claude()
    if not claude or not transcript.strip():
        return None
    # instruction as a single-line arg (cmd.exe mangles newlines in args),
    # transcript on stdin
    # Loom-style naming: a short label a person would actually type, not a
    # description of the video. Judging the content ("no substantive
    # content") is explicitly out — the title is a name, not a review.
    prompt = (
        "Stdin is the transcript of a screen recording. Reply with ONLY a "
        'JSON object {"title": ..., "description": ...}. '
        "Title: 2 to 5 words, like a person naming their own video — "
        "'Introduction', 'Quick intro', 'Checkout bug walkthrough', "
        "'Pricing questions'. Sentence case, no quotes, no trailing period, "
        "no filler like 'screen recording' or 'video'. Never judge or "
        "editorialise the content (never say things like 'no substantive "
        "content' or 'brief'); if the clip is only a greeting, name it for "
        "the greeting. Description: 1-2 plain sentences on what it covers.")
    try:
        r = subprocess.run(
            [*claude, "-p", prompt, "--output-format", "json", "--max-turns", "1"],
            input=transcript[:6000],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW, timeout=120, cwd=str(Path.home()))
        if r.returncode != 0:
            return None
        # Depending on version/flags the CLI prints either the model's reply
        # verbatim, a single result object, or a list of events. Collect every
        # candidate text and take the first that contains our JSON shape.
        candidates = [r.stdout]
        try:
            payload = json.loads(r.stdout)
            if isinstance(payload, dict):
                candidates.append(str(payload.get("result", "")))
            elif isinstance(payload, list):
                for p in payload:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "result":
                        candidates.append(str(p.get("result", "")))
                    elif p.get("type") == "assistant":
                        for block in p.get("message", {}).get("content", []):
                            candidates.append(str(block.get("text", "")))
        except ValueError:
            pass
        for text in candidates:
            m = re.search(r'\{[^{}]*"title"[^{}]*\}', text, re.DOTALL)
            if not m:
                continue
            try:
                data = json.loads(m.group(0))
            except ValueError:
                continue
            if data.get("title"):
                return {"title": str(data["title"])[:200],
                        "description": str(data.get("description", ""))[:2000]}
    except Exception as exc:
        log.warning("ai_title failed: %s", exc)
    return None


def ai_chapters(segments: list[dict], duration: float) -> list | None:
    """Chapter list from the timed transcript, via the claude CLI."""
    claude = _find_claude()
    if not claude or len(segments) < 4 or duration < 60:
        return None
    lines = "\n".join(f"[{int(s['start'] // 60)}:{int(s['start'] % 60):02d}] "
                      f"{s['text']}" for s in segments)
    prompt = (
        "Stdin is a timestamped transcript of a screen recording. Split it "
        "into 3-8 chapters. Reply with ONLY a JSON array like "
        '[{"t": 0, "title": "Intro"}, ...] where t is the chapter start in '
        "seconds (integer, first chapter t=0) and title is at most 5 words, "
        "sentence case.")
    try:
        r = subprocess.run(
            [*claude, "-p", prompt, "--output-format", "json",
             "--max-turns", "1"],
            input=lines[:8000],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW, timeout=120, cwd=str(Path.home()))
        if r.returncode != 0:
            return None
        candidates = [r.stdout]
        try:
            payload = json.loads(r.stdout)
            if isinstance(payload, dict):
                candidates.append(str(payload.get("result", "")))
            elif (isinstance(payload, list) and payload
                  and isinstance(payload[0], dict) and "t" in payload[0]):
                candidates.insert(0, r.stdout)
            elif isinstance(payload, list):
                for p in payload:
                    if isinstance(p, dict) and p.get("type") == "result":
                        candidates.append(str(p.get("result", "")))
        except ValueError:
            pass
        for text in candidates:
            m = re.search(r"\[\s*\{.*?\}\s*\]", text, re.DOTALL)
            if not m:
                continue
            try:
                chapters = json.loads(m.group(0))
            except ValueError:
                continue
            clean = [{"t": max(0, int(c["t"])), "title": str(c["title"])[:60]}
                     for c in chapters
                     if isinstance(c, dict) and "t" in c and c.get("title")]
            if 2 <= len(clean) <= 12:
                clean.sort(key=lambda c: c["t"])
                return clean
    except Exception as exc:
        log.warning("ai_chapters failed: %s", exc)
    return None


def heuristic_title(transcript: str) -> str:
    words = transcript.strip().split()
    if not words:
        return "Screen recording"
    t = " ".join(words[:9])
    return (t[:57] + "…") if len(t) > 58 else t


def run_pipeline(slug: str, video: Path) -> None:
    take_dir = video.parent
    duration = recorder.probe_duration(video)

    if not _has_audio(video):
        # no mic on this take: no transcript to build on, but the viewer
        # still gets a thumbnail and the edit panel still shows its zeros
        log.info("no audio stream — skipping transcription")
        thumb = make_thumbnail(video, duration)
        if thumb:
            api.upload_asset(slug, "thumb", thumb)
        api.set_meta(slug, title=heuristic_title(""), title_is_ai=False)
        (take_dir / "detect.json").write_text('{"fillers": [], "silences": []}',
                                              "utf-8")
        for kind in ("fillers", "silences", "captions"):
            api.register_edit(slug, kind, 0, False)
        return

    # 1. transcript — everything below depends on it
    try:
        wav = extract_audio(video)
        tr = transcribe(wav)
        wav.unlink(missing_ok=True)
    except Exception:
        log.exception("transcription failed")
        api.report_failure(f"transcription failed for {slug}")
        thumb = make_thumbnail(video, duration)
        if thumb:
            api.upload_asset(slug, "thumb", thumb)
        api.set_meta(slug, title=heuristic_title(""), title_is_ai=False)
        return

    api.set_meta(slug, transcript=tr["text"])
    words_file = take_dir / "words.json"
    words_file.write_text(json.dumps(tr["words"]), "utf-8")
    api.upload_asset(slug, "words", words_file)

    # speaking pace → default playback speed for viewers
    wpm = measure_wpm(tr["words"])
    if wpm:
        speed = best_speed(wpm)
        log.info("speaking pace %.0f wpm → default playback %.2gx", wpm, speed)
        api.set_meta(slug, wpm=round(wpm, 1), default_speed=speed)
    vtt_file = take_dir / "captions.vtt"
    n_cues = write_vtt(tr["segments"], vtt_file, tr["words"])
    api.upload_asset(slug, "vtt", vtt_file)

    # 2. AI title + description
    meta = ai_title(tr["text"])
    if meta:
        api.set_meta(slug, title=meta["title"],
                     description=meta["description"], title_is_ai=True)
    else:
        api.set_meta(slug, title=heuristic_title(tr["text"]), title_is_ai=False)

    # 3. chapters + thumbnail
    chapters = ai_chapters(tr["segments"], duration)
    if chapters:
        api.set_meta(slug, chapters=json.dumps(chapters))
    thumb = make_thumbnail(video, duration)
    if thumb:
        api.upload_asset(slug, "thumb", thumb)
    # waveform for the strip that shows which parts the cuts remove
    peaks = make_peaks(video, duration)
    if peaks:
        api.upload_asset(slug, "peaks", peaks)

    # 4. edit detection — always registered, even at count 0, so the panel
    #    can show "0 found" (proof the detector ran) instead of hiding it
    auto = api.get_auto_apply()
    silence_cuts = detect_silences(tr["segments"], duration, tr["words"])
    # keep anything the waveform says is audible, whatever whisper thought
    if peaks:
        try:
            silence_cuts = refine_silences(
                silence_cuts, json.loads(peaks.read_text("utf-8")))
        except (OSError, ValueError):
            log.warning("could not read peaks for silence refinement")
    filler_cuts = detect_fillers(tr["words"])
    detections = {
        "fillers": filler_cuts,
        "silences": silence_cuts,
    }
    (take_dir / "detect.json").write_text(json.dumps(detections), "utf-8")

    api.register_edit(slug, "fillers", len(filler_cuts),
                      bool(auto.get("fillers") and filler_cuts),
                      {"cuts": filler_cuts})
    api.register_edit(slug, "silences", len(silence_cuts),
                      bool(auto.get("silences") and silence_cuts),
                      {"cuts": silence_cuts})
    api.register_edit(slug, "captions", n_cues,
                      bool(auto.get("captions") and n_cues))

    # Cut renders are the server's job now — it has the source video and the
    # cut lists above, so toggles keep working with this app closed.
