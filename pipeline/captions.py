"""
Captions stage (M6).
timing.json (+ script.json for scene boundaries) -> captions.ass
Word-by-word karaoke captions, burned in later by FFmpeg's ass= filter.

CRITICAL: \\k durations are in CENTISECONDS and must be computed from
CUMULATIVE timestamps, not independent per-word durations — rounding each
word's duration separately causes drift that snowballs into audible/visible
desync over a long line. Every duration below is derived by rounding the
cumulative position and subtracting the previous cumulative rounded value.
"""

import json
import subprocess
import unicodedata
from pathlib import Path

WORDS_PER_LINE_MIN = 3
WORDS_PER_LINE_MAX = 5

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920

MARGIN_V = 300  # distance from bottom, with Alignment=2 (bottom-center)

STYLE_PRESETS = {
    "pop": {
        "karaoke_tag": "k",
        "primary_colour": "&H0000D7FF",
        "secondary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "bold": 1,
    },
    "sweep": {
        "karaoke_tag": "kf",
        "primary_colour": "&H0000D7FF",
        "secondary_colour": "&H00C8C8C8",
        "outline_colour": "&H00000000",
        "bold": 1,
    },
}


class CaptionsError(Exception):
    """Raised when caption generation fails."""


def _normalize_words(text: str) -> list[str]:
    return unicodedata.normalize("NFC", text).split()


def _assign_scenes_to_words(script: dict, words: list[dict]) -> list[dict]:
    scene_word_counts = [len(_normalize_words(s["text"])) for s in script["scenes"]]

    out = []
    idx = 0
    for scene_i, count in enumerate(scene_word_counts):
        for _ in range(count):
            if idx >= len(words):
                break
            w = dict(words[idx])
            w["scene_index"] = scene_i
            out.append(w)
            idx += 1
    while idx < len(words):
        w = dict(words[idx])
        w["scene_index"] = len(scene_word_counts) - 1
        out.append(w)
        idx += 1
    return out


def _group_into_lines(words: list[dict]) -> list[list[dict]]:
    lines = []
    current = []
    for w in words:
        if current and (
            w["scene_index"] != current[-1]["scene_index"]
            or len(current) >= WORDS_PER_LINE_MAX
        ):
            lines.append(current)
            current = []
        current.append(w)
        if len(current) >= WORDS_PER_LINE_MIN and len(current) >= WORDS_PER_LINE_MAX:
            lines.append(current)
            current = []
    if current:
        lines.append(current)
    return lines


def _seconds_to_ass_time(seconds: float) -> str:
    cs_total = round(seconds * 100)
    h = cs_total // 360000
    cs_total -= h * 360000
    m = cs_total // 6000
    cs_total -= m * 6000
    s = cs_total // 100
    cs = cs_total % 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _build_karaoke_text(line: list[dict], tag: str) -> str:
    line_start = line[0]["start"]
    parts = []
    cum_prev_cs = 0

    for i, w in enumerate(line):
        if i < len(line) - 1:
            boundary = line[i + 1]["start"]
        else:
            boundary = w["end"]
        cum_cs = round((boundary - line_start) * 100)
        k = max(cum_cs - cum_prev_cs, 1)
        cum_prev_cs = cum_cs
        parts.append(f"{{\\{tag}{k}}}{w['w']}")

    return " ".join(parts)


def generate_ass(script: dict, timing: dict, style_name: str, font: str, font_size: int) -> str:
    if style_name not in STYLE_PRESETS:
        raise CaptionsError(f"Unknown caption style '{style_name}'. Options: {list(STYLE_PRESETS)}")
    preset = STYLE_PRESETS[style_name]

    words = _assign_scenes_to_words(script, timing["words"])
    if not words:
        raise CaptionsError("No words to caption — timing.json was empty.")

    lines = _group_into_lines(words)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VIDEO_WIDTH}
PlayResY: {VIDEO_HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},{preset['primary_colour']},{preset['secondary_colour']},{preset['outline_colour']},&H00000000,{preset['bold']},0,0,0,100,100,0,0,1,3,0,2,40,40,{MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    for line in lines:
        start = _seconds_to_ass_time(line[0]["start"])
        end = _seconds_to_ass_time(line[-1]["end"])
        text = _build_karaoke_text(line, preset["karaoke_tag"])
        events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    return header + "\n".join(events) + "\n"


def run_captions(job_dir: Path, config: dict) -> dict:
    script_path = job_dir / "script.json"
    timing_path = job_dir / "timing.json"
    if not script_path.exists():
        raise CaptionsError("script.json not found — run script generation stage first.")
    if not timing_path.exists():
        raise CaptionsError("timing.json not found — run alignment stage first.")

    script = json.loads(script_path.read_text(encoding="utf-8"))
    timing = json.loads(timing_path.read_text(encoding="utf-8"))

    language = config.get("language", "en")
    style_name = config.get("captions", {}).get("style", "pop")
    font = config.get("captions", {}).get("font", {}).get(language, "Arial")
    font_size = 76 if language == "en" else int(76 * 1.15)

    ass_content = generate_ass(script, timing, style_name, font, font_size)

    ass_path = job_dir / "captions.ass"
    ass_path.write_text(ass_content, encoding="utf-8")

    line_count = ass_content.count("Dialogue:")
    return {"style": style_name, "font": font, "line_count": line_count}


def preview(job_id: str):
    """
    CLI helper: python -m pipeline.captions preview <job_id>
    Burns captions.ass over a black clip so you can visually check styling
    without doing a full render.
    """
    job_dir = Path("jobs") / job_id
    ass_path = job_dir / "captions.ass"
    if not ass_path.exists():
        print(f"No captions.ass found in {job_dir}. Run the captions stage first.")
        return

    timing_path = job_dir / "timing.json"
    duration = 60.0
    if timing_path.exists():
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        if timing["words"]:
            duration = timing["words"][-1]["end"] + 1.0

    output_path = job_dir / "captions_preview.mp4"

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c=black:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:d={duration}",
                "-vf", "ass=captions.ass",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(output_path.name),
            ],
            cwd=str(job_dir),
            check=True,
        )
        print(f"Preview written to {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"Preview render failed: {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "preview":
        preview(sys.argv[2])
    else:
        print("Usage: python -m pipeline.captions preview <job_id>")