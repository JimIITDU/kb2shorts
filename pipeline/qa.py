"""
QA gate stage (M8).
Automated pass/fail checks on the rendered video before packaging.
A failing check must stop packaging — this stage exists specifically to
catch problems that "the CLI didn't crash" doesn't prove.
"""

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

MIN_DURATION = 35
MAX_DURATION = 62
LOUDNESS_TARGET = -14
LOUDNESS_TOLERANCE = 1


class QaError(Exception):
    """Raised when the QA gate fails — job must not be packaged."""


def _ffprobe_json(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def _check_duration(probe: dict) -> tuple[bool, str, float]:
    duration = float(probe["format"]["duration"])
    ok = MIN_DURATION <= duration <= MAX_DURATION
    return ok, f"{duration:.2f}s (need {MIN_DURATION}-{MAX_DURATION}s)", duration


def _check_video_format(probe: dict) -> tuple[bool, str]:
    video_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        return False, "no video stream found"
    v = video_streams[0]
    ok = (
        v.get("codec_name") == "h264"
        and v.get("width") == 1080
        and v.get("height") == 1920
        and v.get("pix_fmt") == "yuv420p"
    )
    detail = f"{v.get('width')}x{v.get('height')} {v.get('codec_name')} {v.get('pix_fmt')}"
    return ok, detail


def _check_loudness(video_path: Path) -> tuple[bool, str]:
    result = subprocess.run(
        [
            "ffmpeg", "-i", str(video_path),
            "-af", "loudnorm=print_format=json",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=60,
    )
    match = re.search(r"\{[^{}]*\}", result.stderr, re.DOTALL)
    if not match:
        return False, "could not measure loudness"
    stats = json.loads(match.group(0))
    input_i = float(stats["input_i"])
    ok = abs(input_i - LOUDNESS_TARGET) <= LOUDNESS_TOLERANCE
    return ok, f"{input_i:.2f} LUFS (need {LOUDNESS_TARGET}\u00b1{LOUDNESS_TOLERANCE})"


def _check_not_frozen(video_path: Path, duration: float) -> tuple[bool, str]:
    """Sample 5 evenly-spaced frames; fail if all are byte-identical (frozen video)."""
    if duration <= 1:
        return False, "video too short to sample"

    timestamps = [duration * f for f in (0.1, 0.3, 0.5, 0.7, 0.9)]
    hashes = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for i, t in enumerate(timestamps):
            frame_path = tmp_path / f"frame_{i}.png"
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-ss", str(t), "-i", str(video_path),
                        "-frames:v", "1", str(frame_path),
                    ],
                    capture_output=True, timeout=15, check=True,
                )
                hashes.append(hashlib.sha256(frame_path.read_bytes()).hexdigest())
            except Exception:
                hashes.append(None)

    valid_hashes = [h for h in hashes if h is not None]
    if len(valid_hashes) < 2:
        return False, "could not extract enough sample frames"

    all_identical = len(set(valid_hashes)) == 1
    ok = not all_identical
    return ok, (
        "sampled frames differ across the timeline" if ok
        else "all sampled frames are byte-identical — video appears frozen"
    )


def _ass_time_to_seconds(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _check_captions_cover_timing(job_dir: Path) -> tuple[bool, str]:
    """
    Structural check, per the manual: pick words from timing.json and verify
    captions.ass has a Dialogue line whose [Start,End] window covers each
    word's midpoint. This proves the ASS file wasn't stale or mismatched
    against the actual audio timing — it does NOT verify visual sync on
    screen, which the manual explicitly leaves to the human rubric.
    """
    timing_path = job_dir / "timing.json"
    captions_path = job_dir / "captions.ass"
    if not timing_path.exists() or not captions_path.exists():
        return False, "timing.json or captions.ass missing"

    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    words = timing.get("words", [])
    if not words:
        return False, "no words in timing.json"

    sample_indices = sorted(set(
        max(0, min(len(words) - 1, int(len(words) * f)))
        for f in (0.1, 0.3, 0.5, 0.7, 0.9)
    ))
    sample_words = [words[i] for i in sample_indices]

    ass_text = captions_path.read_text(encoding="utf-8")
    dialogue_windows = []
    for line in ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 3:
            continue
        try:
            start = _ass_time_to_seconds(parts[1].strip())
            end = _ass_time_to_seconds(parts[2].strip())
        except ValueError:
            continue
        dialogue_windows.append((start, end))

    missing = []
    for w in sample_words:
        midpoint = (w["start"] + w["end"]) / 2
        covered = any(start <= midpoint <= end for start, end in dialogue_windows)
        if not covered:
            missing.append(w["w"])

    ok = not missing
    return ok, (
        "all sampled word timestamps fall inside a caption line" if ok
        else f"words not covered by any caption line: {missing}"
    )


def _check_plays_fully(video_path: Path) -> tuple[bool, str]:
    """Decode the entire file to null; any decode error means it's corrupt/unplayable."""
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video_path), "-f", "null", "-"],
            capture_output=True, timeout=120, check=True,
        )
        return True, "decoded end-to-end without errors"
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else ""
        return False, f"decode error: {stderr[-300:]}"
    except subprocess.TimeoutExpired:
        return False, "decode timed out after 120s"


def run_qa(job_dir: Path, config: dict) -> dict:
    video_path = job_dir / "video.mp4"
    if not video_path.exists():
        raise QaError("video.mp4 not found — run the render stage first.")

    report_path = job_dir / "qa_report.json"

    try:
        probe = _ffprobe_json(video_path)
    except Exception as e:
        # Write a report reflecting THIS run's true state before raising —
        # otherwise a stale PASSED report from a previous run is left on
        # disk and gets misread (by cli.py, or anyone opening the file) as
        # still current, even though the verdict just flipped to failed.
        report = {
            "passed": False,
            "checks": [
                {
                    "name": "ffprobe_readable",
                    "passed": False,
                    "detail": f"ffprobe failed to read video.mp4: {e}",
                },
            ],
        }
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        raise QaError(f"ffprobe failed to read video.mp4: {e}")

    checks = []

    dur_ok, dur_detail, duration = _check_duration(probe)
    checks.append(("duration", dur_ok, dur_detail))

    fmt_ok, fmt_detail = _check_video_format(probe)
    checks.append(("video_format", fmt_ok, fmt_detail))

    loud_ok, loud_detail = _check_loudness(video_path)
    checks.append(("loudness", loud_ok, loud_detail))

    frozen_ok, frozen_detail = _check_not_frozen(video_path, duration)
    checks.append(("not_frozen", frozen_ok, frozen_detail))

    caption_ok, caption_detail = _check_captions_cover_timing(job_dir)
    checks.append(("captions_cover_timing", caption_ok, caption_detail))

    plays_ok, plays_detail = _check_plays_fully(video_path)
    checks.append(("plays_fully", plays_ok, plays_detail))

    all_passed = all(passed for _, passed, _ in checks)

    report = {
        "passed": all_passed,
        "checks": [
            {"name": name, "passed": passed, "detail": detail}
            for name, passed, detail in checks
        ],
    }

    report_path = job_dir / "qa_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if not all_passed:
        failed = [c["name"] for c in report["checks"] if not c["passed"]]
        raise QaError(f"QA failed: {failed}. See qa_report.json for details.")

    return report