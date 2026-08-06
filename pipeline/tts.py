"""
Voice stage (M3).
script.json -> voice.wav (mono, 24kHz), using the TTS provider behind TTSClient.
"""

import json
import subprocess
from pathlib import Path

from pipeline.providers.tts import TTSClient, TTSError

MIN_DURATION_SECONDS = 35
MAX_DURATION_SECONDS = 65


class VoiceGenError(Exception):
    """Raised when voice generation fails or produces an out-of-bounds result."""


def build_narration_text(script: dict) -> str:
    parts = []
    for scene in script["scenes"]:
        text = scene["text"].strip()
        if text and text[-1] not in ".!?।":
            text += "."
        parts.append(text)
    return " ".join(parts)


def get_audio_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return float(result.stdout.strip())
    except Exception as e:
        raise VoiceGenError(f"Could not read audio duration via ffprobe: {e}")


def convert_to_wav(mp3_path: Path, wav_path: Path):
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(mp3_path),
                "-ar", "24000", "-ac", "1",
                str(wav_path),
            ],
            capture_output=True, timeout=60, check=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else ""
        raise VoiceGenError(f"ffmpeg mp3->wav conversion failed: {stderr[-500:]}")


def run_voice_gen(job_dir: Path, config: dict) -> dict:
    script_path = job_dir / "script.json"
    if not script_path.exists():
        raise VoiceGenError("script.json not found — run script generation stage first.")
    script = json.loads(script_path.read_text(encoding="utf-8"))

    language = config.get("language", "en")
    voice_name = config.get("tts", {}).get("voices", {}).get(language)

    narration_text = build_narration_text(script)

    client = TTSClient(language=language, voice=voice_name)

    mp3_path = job_dir / "voice_raw.mp3"
    wav_path = job_dir / "voice.wav"

    try:
        client.synthesize(narration_text, mp3_path)
    except TTSError as e:
        raise VoiceGenError(str(e))

    convert_to_wav(mp3_path, wav_path)

    duration = get_audio_duration(wav_path)

    if duration < MIN_DURATION_SECONDS or duration > MAX_DURATION_SECONDS:
        raise VoiceGenError(
            f"Voice duration {duration:.1f}s is outside the allowed "
            f"{MIN_DURATION_SECONDS}-{MAX_DURATION_SECONDS}s range."
        )

    mp3_path.unlink(missing_ok=True)

    return {
        "duration_seconds": round(duration, 2),
        "voice": client.voice,
        "language": language,
    }