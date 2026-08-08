"""
Render stage (M7).
Combines background clips + voice audio + captions + progress bar into
the final video.mp4, using a single generated FFmpeg filtergraph.

Current scope (P0): hard cuts between scenes (no crossfades yet — that's
a fast-follow), captions burned in, progress bar, loudness-normalized
voice audio. Background music mixing is deferred until a curated music
library exists (per the brief, we never pull music from an unlicensed
or unimplementable source).
"""

import json
import re
import subprocess
import unicodedata
from pathlib import Path

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
TARGET_FPS = 30

LOUDNESS_TARGET_I = -14
LOUDNESS_TARGET_TP = -1.5
LOUDNESS_TARGET_LRA = 11

# Light compression before loudnorm, per the brief's "Voiceover Processing"
# spec. Edge-TTS output can have a wide gap between average loudness and
# peak level (observed: -20.8 LUFS integrated vs -2.8 dBTP peak, an 18 dB
# gap). Raising that straight to -14 LUFS with flat gain would push peaks
# past the -1.5 dBTP ceiling, forcing loudnorm to silently fall back to its
# less-precise "dynamic" mode instead of "linear" — which is why single-
# and two-pass runs without this were landing at -15.6 / -15.1, just
# outside the -14 +-1 tolerance. Taming the peaks first gives loudnorm
# enough headroom to apply true linear gain and land on target.
AUDIO_PREPROCESS_FILTER = "acompressor=threshold=-20dB:ratio=3:attack=5:release=100:makeup=2dB"


class RenderError(Exception):
    """Raised when the render fails."""


def _normalize_words(text: str) -> list[str]:
    return unicodedata.normalize("NFC", text).split()


def _get_audio_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, timeout=15, check=True,
    )
    return float(result.stdout.strip())


def _measure_loudness(voice_path: Path) -> dict:
    """
    First pass of two-pass loudnorm.

    Single-pass loudnorm (apply loudnorm=I=-14:TP=-1.5:LRA=11 directly
    during the main render) is measurably less accurate — FFmpeg's own
    docs note it estimates correction from a lookahead window rather than
    the whole file. In testing this missed the QA gate's -14 +-1 LUFS
    target by over half a dB (-15.61 measured). Two-pass — measure real
    input stats here, then feed those exact measured values into the
    correction filter in the main render — reliably lands inside tolerance.
    """
    cmd = [
        "ffmpeg", "-i", str(voice_path),
        "-af", (
            f"{AUDIO_PREPROCESS_FILTER},"
            f"loudnorm=I={LOUDNESS_TARGET_I}:TP={LOUDNESS_TARGET_TP}:"
            f"LRA={LOUDNESS_TARGET_LRA}:print_format=json"
        ),
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    # loudnorm writes its JSON measurement block to stderr, mixed in with
    # normal ffmpeg log lines — extract just the {...} block.
    match = re.search(r"\{[^{}]*\}", result.stderr, re.DOTALL)
    if not match:
        raise RenderError(
            "Could not find loudnorm measurement output in ffmpeg stderr "
            "(measurement pass may have failed to run)."
        )
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise RenderError(f"Failed to parse loudnorm measurement JSON: {e}")


def _build_loudnorm_filter(stats: dict) -> str:
    """
    Second pass: apply loudnorm using the values measured in pass one
    (measured_I/TP/LRA/thresh + offset), with linear=true so the
    correction is a precise gain adjustment rather than another estimate.
    """
    return (
        f"loudnorm=I={LOUDNESS_TARGET_I}:TP={LOUDNESS_TARGET_TP}:"
        f"LRA={LOUDNESS_TARGET_LRA}:"
        f"measured_I={stats['input_i']}:"
        f"measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:"
        f"measured_thresh={stats['input_thresh']}:"
        f"offset={stats['target_offset']}:"
        "linear=true:print_format=summary"
    )


def _compute_scene_segments(script: dict, timing: dict, audio_duration: float) -> list[tuple[float, float]]:
    """
    Computes each scene's ON-SCREEN segment (start, end) covering the FULL
    audio timeline, including natural pauses between scenes — not just the
    span of that scene's own words. Otherwise the background video is
    shorter than the voice track (only word-span duration, missing the
    silence gaps) and -shortest would truncate the final narration.

    Scene 0 starts at t=0 (covers any lead-in silence before the first word).
    Each scene i's segment ends where scene i+1's first word begins.
    The last scene extends all the way to the actual voice.wav duration.
    """
    words = timing["words"]
    scene_word_counts = [len(_normalize_words(s["text"])) for s in script["scenes"]]

    first_word_starts = []
    idx = 0
    for count in scene_word_counts:
        scene_words = words[idx: idx + count]
        if not scene_words:
            raise RenderError("A scene had no aligned words — cannot determine its duration.")
        first_word_starts.append(scene_words[0]["start"])
        idx += count

    segments = []
    for i in range(len(first_word_starts)):
        start = 0.0 if i == 0 else first_word_starts[i]
        end = first_word_starts[i + 1] if i + 1 < len(first_word_starts) else audio_duration
        segments.append((start, end))

    return segments


def _build_filtergraph(
    asset_paths: list[str],
    scene_durations: list[float],
    captions_path: str,
    total_duration: float,
) -> tuple[list[str], str]:
    """
    Builds the ffmpeg -filter_complex string and returns (input_args, filter_str).
    Each scene's background is trimmed/looped to its scene duration, scaled
    and cropped to fill 1080x1920, normalized to 30fps/yuv420p, then
    concatenated with hard cuts. Captions and the progress bar are applied
    after concatenation.
    """
    input_args = []
    per_scene_filters = []

    for i, (path, duration) in enumerate(zip(asset_paths, scene_durations)):
        input_args += ["-stream_loop", "-1", "-t", str(duration), "-i", path]
        # scale to fill (cropping excess), force fps/pix_fmt so concat is safe
        per_scene_filters.append(
            f"[{i}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps={TARGET_FPS},format=yuv420p,setsar=1[v{i}]"
        )

    concat_inputs = "".join(f"[v{i}]" for i in range(len(asset_paths)))
    # CRITICAL: setpts=PTS-STARTPTS resets timestamps to start cleanly at 0
    # and increase monotonically. Without this, per-scene clips built with
    # -stream_loop can leave the concatenated stream with irregular PTS,
    # which corrupts any downstream filter that reads frame timestamps
    # (e.g. geq's T variable in the progress bar) — causing it to briefly
    # read wrong values at each scene boundary.
    concat_filter = (
        f"{concat_inputs}concat=n={len(asset_paths)}:v=1:a=0,"
        f"setpts=PTS-STARTPTS[concatenated]"
    )

    # Captions burn-in
    captions_filter = f"[concatenated]ass={captions_path}[captioned]"

    # Progress bar: thin bar along the bottom, width grows with elapsed time.
    # Uses geq (not drawbox) — geq's pixel expressions are ALWAYS evaluated
    # per-frame by design, whereas drawbox's dynamic-width support varies
    # unreliably across FFmpeg builds (some require an eval=frame option
    # that doesn't exist in every build; without it, drawbox silently
    # freezes the bar at its initial size for the entire video instead of
    # growing). geq sidesteps that inconsistency entirely.
    #
    # PERFORMANCE: running geq's per-pixel conditional over the FULL
    # 1080x1920 frame (2M+ pixels) every frame is far too slow. Instead,
    # crop out just the thin 8px-tall strip where the bar lives, run geq
    # on only that tiny region (1080x8 = ~8600 pixels), then overlay it
    # back onto the full frame — cuts the expensive work by ~240x.
    bar_h = 8
    progress_filter = (
        f"[captioned]split=2[main][forbar];"
        f"[forbar]crop={VIDEO_WIDTH}:{bar_h}:0:{VIDEO_HEIGHT}-{bar_h}[barstrip];"
        f"[barstrip]geq="
        f"lum='if(lt(X,W*T/{total_duration}),235,lum(X,Y))':"
        f"cb=128:cr=128[bardone];"
        f"[main][bardone]overlay=0:{VIDEO_HEIGHT}-{bar_h}[final]"
    )

    filter_complex = ";".join(per_scene_filters + [concat_filter, captions_filter, progress_filter])

    return input_args, filter_complex


def run_render(job_dir: Path, config: dict) -> dict:
    script_path = job_dir / "script.json"
    timing_path = job_dir / "timing.json"
    assets_path = job_dir / "assets.json"
    captions_path = job_dir / "captions.ass"
    voice_path = job_dir / "voice.wav"

    for p, name in [
        (script_path, "script.json"), (timing_path, "timing.json"),
        (assets_path, "assets.json"), (captions_path, "captions.ass"),
        (voice_path, "voice.wav"),
    ]:
        if not p.exists():
            raise RenderError(f"{name} not found — run earlier stages first.")

    script = json.loads(script_path.read_text(encoding="utf-8"))
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    assets = json.loads(assets_path.read_text(encoding="utf-8"))

    audio_duration = _get_audio_duration(voice_path)
    segments = _compute_scene_segments(script, timing, audio_duration)
    scene_durations = [round(end - start, 3) for start, end in segments]
    total_duration = audio_duration

    # Asset paths are stored relative to the project root (where the CLI
    # runs). Since we run ffmpeg with cwd=job_dir (to sidestep a Windows
    # bug where an absolute path's "C:" colon is misread as a filter
    # separator inside filter-string arguments like ass=...), resolve
    # these to absolute paths now, before changing directory context.
    # Absolute paths are safe for -i flags (not filter-string arguments),
    # only the ass= filter argument needs to stay a bare relative filename.
    asset_paths = [str(Path(s["asset_path"]).resolve()) for s in assets["scenes"]]
    if len(asset_paths) != len(scene_durations):
        raise RenderError(
            f"Mismatch: {len(asset_paths)} assets but {len(scene_durations)} scenes."
        )

    input_args, filter_complex = _build_filtergraph(
        asset_paths, scene_durations, "captions.ass", total_duration
    )

    output_path = job_dir / "video.mp4"
    log_path = job_dir / "render_ffmpeg_command.log"

    voice_input_index = len(asset_paths)

    # Two-pass loudnorm: measure real input loudness (through the same
    # compression the final render applies — see AUDIO_PREPROCESS_FILTER),
    # then apply correction using those measured values.
    loudness_stats = _measure_loudness(voice_path)
    audio_filter = f"{AUDIO_PREPROCESS_FILTER},{_build_loudnorm_filter(loudness_stats)}"

    cmd = (
        ["ffmpeg", "-y"]
        + input_args
        + ["-i", "voice.wav"]
        + [
            "-filter_complex", filter_complex,
            "-map", "[final]",
            "-map", f"{voice_input_index}:a",
            "-af", audio_filter,
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-shortest",
            "video.mp4",
        ]
    )

    log_path.write_text(
        " ".join(cmd) + f"\n\n# loudnorm measurement pass stats:\n{json.dumps(loudness_stats, indent=2)}",
        encoding="utf-8",
    )

    try:
        result = subprocess.run(
            cmd, cwd=str(job_dir), capture_output=True, timeout=600, check=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else ""
        raise RenderError(f"FFmpeg render failed:\n{stderr[-1500:]}")
    except subprocess.TimeoutExpired:
        raise RenderError("FFmpeg render timed out after 10 minutes.")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RenderError("Render completed but video.mp4 is missing or empty.")

    return {
        "output_path": str(output_path),
        "total_duration": round(total_duration, 2),
        "scene_count": len(asset_paths),
    }