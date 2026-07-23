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

from . import api, config, recorder
from .edits_render import _has_audio, render_cuts

log = logging.getLogger("dr.processing")
CREATE_NO_WINDOW = 0x08000000

FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhm", "ah", "ahh", "er", "erm",
           "hmm", "hm", "mhm", "mm", "like"}
FILLER_BIGRAMS = {("you", "know"), ("i", "mean"), ("sort", "of"), ("kind", "of")}
MIN_SILENCE_S = 1.0
CUT_PAD_S = 0.05

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


def detect_silences(segments: list[dict], duration: float) -> list[list[float]]:
    """Gap-based, as in dragonEditor: inter-segment gaps over threshold."""
    cuts = []
    if not segments:
        return cuts
    if segments[0]["start"] > MIN_SILENCE_S:
        cuts.append([0.0, segments[0]["start"] - CUT_PAD_S])
    for a, b in zip(segments, segments[1:]):
        gap = b["start"] - a["end"]
        if gap > MIN_SILENCE_S:
            cuts.append([a["end"] + CUT_PAD_S, b["start"] - CUT_PAD_S])
    if duration and duration - segments[-1]["end"] > MIN_SILENCE_S:
        cuts.append([segments[-1]["end"] + CUT_PAD_S, duration])
    return cuts


def detect_fillers(words: list[dict]) -> list[list[float]]:
    """Word-level filler cuts from whisper word timestamps."""
    clean = [re.sub(r"[^a-z']", "", w["word"].lower()) for w in words]
    cuts, i = [], 0
    while i < len(clean):
        if i + 1 < len(clean) and (clean[i], clean[i + 1]) in FILLER_BIGRAMS:
            cuts.append([max(0, words[i]["start"] - CUT_PAD_S),
                         words[i + 1]["end"] + CUT_PAD_S])
            i += 2
            continue
        if clean[i] in FILLERS:
            cuts.append([max(0, words[i]["start"] - CUT_PAD_S),
                         words[i]["end"] + CUT_PAD_S])
        i += 1
    return cuts


def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def write_vtt(segments: list[dict], out: Path) -> int:
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{_ts(seg['start'])} --> {_ts(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    out.write_text("\n".join(lines), "utf-8")
    return len(segments)


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
    prompt = (
        "Stdin is the transcript of a screen recording. Reply with ONLY a "
        'JSON object {"title": ..., "description": ...}. Title: max 60 chars, '
        "specific, no quotes, sentence case. Description: 1-2 sentences of "
        "what the recording covers.")
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
    vtt_file = take_dir / "captions.vtt"
    n_cues = write_vtt(tr["segments"], vtt_file)
    api.upload_asset(slug, "vtt", vtt_file)

    # 2. AI title + description
    meta = ai_title(tr["text"])
    if meta:
        api.set_meta(slug, title=meta["title"],
                     description=meta["description"], title_is_ai=True)
    else:
        api.set_meta(slug, title=heuristic_title(tr["text"]), title_is_ai=False)

    # 3. thumbnail
    thumb = make_thumbnail(video, duration)
    if thumb:
        api.upload_asset(slug, "thumb", thumb)

    # 4. edit detection — always registered, even at count 0, so the panel
    #    can show "0 found" (proof the detector ran) instead of hiding it
    auto = api.get_auto_apply()
    silence_cuts = detect_silences(tr["segments"], duration)
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

    # 5. derived renders for the enabled cut set
    enabled = sorted(k for k in ("fillers", "silences")
                     if auto.get(k) and detections[k])
    if enabled:
        produce_render(slug, video, detections, enabled)


def produce_render(slug: str, video: Path, detections: dict,
                   kinds: list[str]) -> None:
    cuts = sorted(c for k in kinds for c in detections.get(k, []))
    if not cuts:
        return
    name = "cut_" + "+".join(sorted(kinds)) + ".mp4"
    out = video.with_name(name)
    try:
        render_cuts(video, out, cuts)
        api.upload_asset(slug, name.removesuffix(".mp4"), out)
    except Exception:
        log.exception("render %s failed", name)
        api.report_failure(f"derived render {name} failed for {slug}")


def poll_render_jobs() -> None:
    """Dashboard toggles can request renders after the fact; the local take
    dir still has the original and the detection lists."""
    for job in api.get_render_jobs():
        slug, kinds = job["slug"], job["kinds"]
        take = _find_local_take(slug)
        if take is None:
            log.info("render job for %s but no local take", slug)
            continue
        detections = json.loads((take / "detect.json").read_text("utf-8"))
        produce_render(slug, take / "video.mp4", detections, kinds)


def _find_local_take(slug: str) -> Path | None:
    for d in config.RECORDINGS_DIR.iterdir():
        marker = d / "slug.txt"
        if marker.exists() and marker.read_text().strip() == slug \
                and (d / "video.mp4").exists() and (d / "detect.json").exists():
            return d
    return None
