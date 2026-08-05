"""
Environment checker for kb2shorts.
Run with: python -m pipeline.doctor
Prints a checklist of pass/fail for everything the pipeline needs.
"""

import shutil
import subprocess
import sys
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
        # ffmpeg -version prints the build config on a line starting with "configuration:"
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


def check_harfbuzz(config_line):
    # HarfBuzz shaping is needed for correct Bengali conjunct rendering.
    # Some distros bundle it without an explicit flag, so this is a soft check.
    has_flag = "--enable-libharfbuzz" in config_line
    record(
        "ffmpeg HarfBuzz flag present",
        has_flag,
        "explicit flag found" if has_flag
        else "not explicitly flagged (may still work — verify manually before Bengali work in M9)",
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
        _, config_line = check_ffmpeg_libass()
        check_harfbuzz(config_line)
    else:
        record("ffmpeg built with libass", False, "skipped, ffmpeg missing")
        record("ffmpeg HarfBuzz flag present", False, "skipped, ffmpeg missing")
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
