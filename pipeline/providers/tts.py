"""
TTS provider interface (M3).
Currently wraps Edge-TTS (free, unofficial Microsoft endpoint — good quality,
no API key needed). Per the project brief, this is prototype-tier, not an
official production dependency, but it's the best free option available.

To swap providers later (e.g. a paid API with native timestamps), only
this file needs to change — the rest of the pipeline talks to TTSClient.
"""

import asyncio
from pathlib import Path

import edge_tts

DEFAULT_VOICES = {
    "en": "en-US-AriaNeural",
    "bn": "bn-BD-NabanitaNeural",
}


class TTSError(Exception):
    """Raised when TTS synthesis fails."""


class TTSClient:
    def __init__(self, language: str, voice: str | None = None):
        self.language = language
        self.voice = voice or DEFAULT_VOICES.get(language)
        if not self.voice:
            raise TTSError(f"No default voice configured for language '{language}'")

    def synthesize(self, text: str, output_path: Path) -> Path:
        """
        Synthesizes `text` to speech, saving as mp3 at output_path.
        Returns the output path. Raises TTSError on failure.
        """
        try:
            asyncio.run(self._synthesize_async(text, output_path))
        except Exception as e:
            raise TTSError(f"Edge-TTS synthesis failed: {e}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise TTSError("Edge-TTS produced an empty or missing output file.")

        return output_path

    async def _synthesize_async(self, text: str, output_path: Path):
        communicate = edge_tts.Communicate(text, self.voice)
        await communicate.save(str(output_path))