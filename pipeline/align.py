"""
Alignment stage (M4).
voice.wav + script.json (known text) -> timing.json (word-level timestamps).

This is FORCED ALIGNMENT, not transcription — we already know what was
said (the script), we just need to know WHEN each word was spoken.
"""

import json
import subprocess
from pathlib import Path

import whisperx

DEVICE = "cpu"  # change to "cuda" once you have a working NVIDIA GPU setup


class AlignError(Exception):
    """Raised when alignment fails or sanity checks don't pass."""


def get_audio_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, timeout=15, check=True,
    )
    return float(result.stdout.strip())


def build_narration_text(script: dict) -> str:
    parts = []
    for scene in script["scenes"]:
        text = scene["text"].strip()
        if text and text[-1] not in ".!?।":
            text += "."
        parts.append(text)
    return " ".join(parts)


def run_alignment(job_dir: Path, config: dict) -> dict:
    script_path = job_dir / "script.json"
    voice_path = job_dir / "voice.wav"

    if not script_path.exists():
        raise AlignError("script.json not found — run script generation stage first.")
    if not voice_path.exists():
        raise AlignError("voice.wav not found — run voice generation stage first.")

    script = json.loads(script_path.read_text(encoding="utf-8"))
    narration_text = build_narration_text(script)
    language = config.get("language", "en")

    duration = get_audio_duration(voice_path)

    try:
        audio = whisperx.load_audio(str(voice_path))
        model_a, metadata = whisperx.load_align_model(language_code=language, device=DEVICE)

        # One segment spanning the whole clip, with the KNOWN text —
        # this is forced alignment, not ASR guessing the words.
        segments = [{"start": 0.0, "end": duration, "text": narration_text}]

        result = whisperx.align(
            segments, model_a, metadata, audio, DEVICE, return_char_alignments=False
        )
    except Exception as e:
        raise AlignError(f"WhisperX alignment failed: {e}")

    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            if "start" in w and "end" in w:
                words.append({"w": w["word"], "start": w["start"], "end": w["end"]})

    if not words:
        raise AlignError("Alignment produced no word timestamps.")

    # --- Sanity checks ---
    for i in range(1, len(words)):
        if words[i]["start"] < words[i - 1]["start"]:
            raise AlignError(f"Timestamps not strictly increasing at word {i} ('{words[i]['w']}').")

    last_end = words[-1]["end"]
    if abs(last_end - duration) > 1.5:
        raise AlignError(
            f"Last word ends at {last_end:.2f}s, audio duration is {duration:.2f}s "
            f"(diff > 1.5s tolerance)."
        )

    script_word_count = len(narration_text.split())
    aligned_word_count = len(words)
    drop_rate = abs(script_word_count - aligned_word_count) / script_word_count
    if drop_rate > 0.05:
        raise AlignError(
            f"Word count mismatch: script had {script_word_count} words, "
            f"aligner produced {aligned_word_count} ({drop_rate:.1%} difference, "
            f"exceeds 5% tolerance)."
        )

    timing = {"alignment_source": "whisperx", "words": words}
    timing_path = job_dir / "timing.json"
    timing_path.write_text(json.dumps(timing, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "word_count": aligned_word_count,
        "last_word_end": round(last_end, 2),
        "audio_duration": round(duration, 2),
    }