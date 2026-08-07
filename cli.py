"""
kb2shorts entrypoint.
Usage: python cli.py generate --input samples/article1.txt --config config.yaml
"""

import argparse
import hashlib
import json
from pathlib import Path

import yaml
from dotenv import load_dotenv

from pipeline.ingest import run_ingest, IngestError
from pipeline.script_gen import run_script_gen, ScriptGenError
from pipeline.tts import run_voice_gen, VoiceGenError
from pipeline.align import run_alignment, AlignError

load_dotenv()

STAGES = ["ingest", "script", "tts", "align", "assets", "captions", "render", "qa", "package"]


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def compute_job_id(input_path: str, config_version: str) -> str:
    """
    Job ID = sha256(article_content + config_version)[:12].
    IMPORTANT: hash file CONTENTS, not the file path — renaming a file
    must not create a duplicate job.
    """
    p = Path(input_path)
    if p.exists():
        content = p.read_text(encoding="utf-8")
    else:
        content = input_path
    payload = (content + config_version).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def load_status(job_dir: Path) -> dict:
    status_file = job_dir / "stage_status.json"
    if status_file.exists():
        return json.loads(status_file.read_text(encoding="utf-8"))
    return {stage: "pending" for stage in STAGES}


def save_status(job_dir: Path, status: dict):
    status_file = job_dir / "stage_status.json"
    status_file.write_text(json.dumps(status, indent=2), encoding="utf-8")


def cmd_generate(args):
    config = load_config(args.config)
    config_version = str(config)

    job_id = compute_job_id(args.input, config_version)
    job_dir = Path("jobs") / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    status = load_status(job_dir)

    if args.force:
        status = {stage: "pending" for stage in STAGES}

    save_status(job_dir, status)

    print(f"Job ID: {job_id}")
    print(f"Job dir: {job_dir}")

    # --- Ingest stage (M1) ---
    if status["ingest"] != "done":
        print("\nRunning ingest stage...")
        try:
            meta = run_ingest(args.input, job_dir)
            status["ingest"] = "done"
            save_status(job_dir, status)
            print(f"  OK — {meta['word_count']} words, source: {meta['source_type']}")
            if meta["warning"]:
                print(f"  WARNING: {meta['warning']}")
        except IngestError as e:
            status["ingest"] = "failed"
            save_status(job_dir, status)
            print(f"  FAILED: {e}")
            print("\nStopping — fix the input and re-run.")
            return
    else:
        print("\nIngest already done, skipping (use --force to redo).")

    # --- Script generation stage (M2) ---
    if status["script"] != "done":
        print("\nRunning script generation stage...")
        try:
            result = run_script_gen(job_dir, config)
            status["script"] = "done"
            save_status(job_dir, status)
            print(f"  OK — {result['word_count']} words, {result['scenes']} scenes, "
                  f"{result['attempts']} attempt(s)")
            print(f"  Token usage: {result['token_usage']}")
        except ScriptGenError as e:
            status["script"] = "failed"
            save_status(job_dir, status)
            print(f"  FAILED: {e}")
            print("\nStopping — fix the issue and re-run.")
            return
    else:
        print("Script generation already done, skipping (use --force to redo).")

    # --- Voice generation stage (M3) ---
    if status["tts"] != "done":
        print("\nRunning voice generation stage...")
        try:
            result = run_voice_gen(job_dir, config)
            status["tts"] = "done"
            save_status(job_dir, status)
            print(f"  OK — {result['duration_seconds']}s audio, voice: {result['voice']}")
        except VoiceGenError as e:
            status["tts"] = "failed"
            save_status(job_dir, status)
            print(f"  FAILED: {e}")
            print("\nStopping — fix the issue and re-run.")
            return
    else:
        print("Voice generation already done, skipping (use --force to redo).")

    # --- Alignment stage (M4) ---
    if status["align"] != "done":
        print("\nRunning alignment stage...")
        try:
            result = run_alignment(job_dir, config)
            status["align"] = "done"
            save_status(job_dir, status)
            print(f"  OK — {result['word_count']} words aligned, "
                  f"last word ends {result['last_word_end']}s "
                  f"(audio is {result['audio_duration']}s)")
        except AlignError as e:
            status["align"] = "failed"
            save_status(job_dir, status)
            print(f"  FAILED: {e}")
            print("\nStopping — fix the issue and re-run.")
            return
    else:
        print("Alignment already done, skipping (use --force to redo).")

    print("\nStage status:")
    for stage in STAGES:
        print(f"  {stage}: {status[stage]}")

    still_pending = [s for s in STAGES if status[s] == "pending"]
    print(f"\nNot yet implemented (lands in later milestones): {still_pending}")


def main():
    parser = argparse.ArgumentParser(prog="kb2shorts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen = subparsers.add_parser("generate", help="Generate a video from an article")
    gen.add_argument("--input", required=True, help="Path to article file or a URL")
    gen.add_argument("--config", required=True, help="Path to config.yaml")
    gen.add_argument("--force", action="store_true", help="Redo all stages even if already done")
    gen.set_defaults(func=cmd_generate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()