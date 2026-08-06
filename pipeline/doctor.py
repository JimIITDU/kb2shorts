"""
Environment checker for kb2shorts.
Run with: python -m pipeline.doctor
Prints a checklist of pass/fail for everything the pipeline needs.
"""

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
import os

load_dotenv()

CHECK = "\u2705"
CROSS = "\u274c"

results = []  # (name, passed: bool, detail: str)


def record(name, passed, detail=""):
    results.append((name, passed, detail))


def check_python_version():
    ok = sys.version_info >= (3, 11)
    record(
        "Python >= 3.11",
        ok,
        f"found {sys.version.split()[0]}",
    )


def check_ffmpeg_present():
    path = shutil.which("ffmpeg")
    record("ffmpeg installed", path is not None, path or "not found on PATH")
    return path is not None


def check_ffmpeg_libass():
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10
        )
        config_line = ""
        for line in out.stdout.splitlines():
            if line.strip().startswith("configuration:"):
                config_line = line
                break
        has_libass = "--enable-libass" in config_line
        record(
            "ffmpeg built with libass",
            has_libass,
            "found --enable-libass" if has_libass else "libass flag not found in build config",
        )
        return has_libass, config_line
    except FileNotFoundError:
        record("ffmpeg built with libass", False, "ffmpeg not found, skipped")
        return False, ""
    except Exception as e:
        record("ffmpeg built with libass", False, f"error: {e}")
        return False, ""


def check_bengali_font():
    """
    Looks for a Bengali-capable font (Noto Sans Bengali) actually installed
    on this machine. Tries fontconfig (fc-list) first — standard on Linux/CI —
    and falls back to scanning known font directories on Windows/macOS,
    since fc-list usually isn't present there.
    """
    target = "notosansbengali"

    fc_list = shutil.which("fc-list")
    if fc_list:
        try:
            out = subprocess.run(
                [fc_list], capture_output=True, text=True, timeout=10
            )
            found = target in out.stdout.lower().replace(" ", "")
            record(
                "Noto Sans Bengali font installed",
                found,
                "found via fc-list" if found else "not found via fc-list — install it before M6/M9",
            )
            return
        except Exception as e:
            record("Noto Sans Bengali font installed", False, f"fc-list error: {e}")
            return

    # No fontconfig (typical on plain Windows) — scan common font directories.
    candidate_dirs = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
        Path("/Library/Fonts"),
        Path.home() / "Library/Fonts",
        Path("/usr/share/fonts"),
        Path.home() / ".fonts",
    ]
    found_path = None
    for d in candidate_dirs:
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if target in f.name.lower().replace(" ", ""):
                found_path = f
                break
        if found_path:
            break

    record(
        "Noto Sans Bengali font installed",
        found_path is not None,
        f"found: {found_path}" if found_path
        else "not found — fc-list unavailable (likely Windows); scanned standard font "
             "folders and found nothing. Download Noto Sans Bengali and install it "
             "before M6/M9.",
    )


def _try_ascii_ass_render(ffmpeg: str, tmp_path: Path) -> bool:
    """Sanity check: does the ass filter work at all with plain ASCII text
    and the default (Arial) font? Isolates generic path/filter bugs from
    Bengali-specific font/shaping bugs."""
    ascii_ass = tmp_path / "ascii_test.ass"
    ascii_png = tmp_path / "ascii.png"
    ascii_ass.write_text(
        "[Script Info]\nPlayResX: 640\nPlayResY: 360\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,0,5,10,10,10,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Test\n",
        encoding="utf-8",
    )
    try:
        # Run with cwd=tmp_path and reference the file by bare name — this
        # sidesteps the Windows drive-letter-colon-vs-filter-separator bug
        # entirely, instead of trying to escape it.
        subprocess.run(
            [
                ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=1",
                "-vf", "ass=ascii_test.ass",
                "-frames:v", "1", "ascii.png",
            ],
            capture_output=True, timeout=15, check=True, cwd=str(tmp_path),
        )
        return ascii_png.exists()
    except Exception:
        return False


def check_harfbuzz_render():
    """
    The manual's actual spec: render a one-frame test of a Bengali conjunct
    string through the ass filter and verify it produces a visibly different
    frame than blank — not just grep a build flag, which can pass even when
    shaping is broken.

    Method: burn "ক্ষমতা" onto a solid color frame via libass, and separately
    render the same solid color with no subtitle. If the two output frames
    are byte-identical, the text never actually rendered (broken shaping,
    missing glyphs, or libass not doing its job) — fail.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        record("HarfBuzz/Bengali render test", False, "skipped, ffmpeg missing")
        return

    test_word = "ক্ষমতা"  # contains a conjunct — decomposes visibly if shaping is broken

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ass_path = tmp_path / "test.ass"
        blank_png = tmp_path / "blank.png"
        text_png = tmp_path / "text.png"

        ass_content = f"""[Script Info]
PlayResX: 640
PlayResY: 360

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans Bengali,48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,5,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{test_word}
"""
        ass_path.write_text(ass_content, encoding="utf-8")

        try:
            subprocess.run(
                [
                    ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=1",
                    "-frames:v", "1", "blank.png",
                ],
                capture_output=True, timeout=15, check=True, cwd=str(tmp_path),
            )
            subprocess.run(
                [
                    ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=1",
                    "-vf", "ass=test.ass",
                    "-frames:v", "1", "text.png",
                ],
                capture_output=True, timeout=15, check=True, cwd=str(tmp_path),
            )
        except subprocess.CalledProcessError as e:
            stderr_text = e.stderr.decode(errors="ignore") if e.stderr else ""
            # Keep only the last chunk — ffmpeg's actual error is always at
            # the end, the banner/config lines at the top are noise here.
            tail = stderr_text.strip().splitlines()[-15:]

            # Diagnostic: does a PLAIN ASCII ass render also fail? If so, the
            # problem is generic (path escaping on Windows, filter syntax) and
            # has nothing to do with Bengali/HarfBuzz specifically.
            ascii_ok = _try_ascii_ass_render(ffmpeg, tmp_path)
            hint = (
                "  (Plain ASCII text through the same ass filter ALSO failed — "
                "this is a generic path/filter problem, not Bengali-specific. "
                "Check the .ass file path for spaces or special characters.)"
                if not ascii_ok else
                "  (Plain ASCII text through the ass filter worked fine — "
                "this failure is specific to the Bengali text/font, not the "
                "filter pipeline itself.)"
            )
            record(
                "HarfBuzz/Bengali render test", False,
                "ffmpeg render failed:\n    " + "\n    ".join(tail) + "\n" + hint,
            )
            return
        except Exception as e:
            record("HarfBuzz/Bengali render test", False, f"error: {e}")
            return

        if not blank_png.exists() or not text_png.exists():
            record("HarfBuzz/Bengali render test", False, "output frame(s) missing")
            return

        blank_hash = hashlib.sha256(blank_png.read_bytes()).hexdigest()
        text_hash = hashlib.sha256(text_png.read_bytes()).hexdigest()

        rendered = blank_hash != text_hash
        record(
            "HarfBuzz/Bengali render test",
            rendered,
            "conjunct text visibly rendered" if rendered
            else "frame identical to blank — text did not render "
                 "(check libass HarfBuzz build + font install)",
        )


def check_api_keys():
    required = ["OPENAI_API_KEY"]
    missing = [k for k in required if not os.getenv(k)]
    record(
        "Required API keys present",
        len(missing) == 0,
        "all present" if not missing else f"missing: {', '.join(missing)}",
    )


def check_jobs_writable():
    jobs_dir = Path("jobs")
    try:
        jobs_dir.mkdir(exist_ok=True)
        test_file = jobs_dir / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        record("jobs/ directory writable", True)
    except Exception as e:
        record("jobs/ directory writable", False, str(e))


def main():
    check_python_version()
    ffmpeg_ok = check_ffmpeg_present()
    if ffmpeg_ok:
        check_ffmpeg_libass()
        check_harfbuzz_render()
    else:
        record("ffmpeg built with libass", False, "skipped, ffmpeg missing")
        record("HarfBuzz/Bengali render test", False, "skipped, ffmpeg missing")
    check_bengali_font()
    check_api_keys()
    check_jobs_writable()

    print("\nkb2shorts environment check\n" + "-" * 40)
    all_passed = True
    for name, passed, detail in results:
        icon = CHECK if passed else CROSS
        print(f"{icon} {name}" + (f" — {detail}" if detail else ""))
        if not passed:
            all_passed = False

    print("-" * 40)
    if all_passed:
        print("All checks passed. Ready to build.")
    else:
        print("Some checks failed — fix these before continuing to M1.")
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()