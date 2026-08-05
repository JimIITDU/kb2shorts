"""
kb2shorts entrypoint.
Usage: python cli.py generate --input samples/article1.txt --config config.yaml
"""

import argparse
import hashlib
import json
from pathlib import Path

import yaml

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
        # Treat as a URL — hash the URL string itself for now.
        # (Real URL fetching happens in M1.)
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
    config_version = str(config)  # crude version fingerprint for now

    job_id = compute_job_id(args.input, config_version)
    job_dir = Path("jobs") / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    status = load_status(job_dir)

    if args.force:
        status = {stage: "pending" for stage in STAGES}

    save_status(job_dir, status)

    print(f"Job ID: {job_id}")
    print(f"Job dir: {job_dir}")
    print("\nStage status:")
    for stage in STAGES:
        print(f"  {stage}: {status[stage]}")

    unimplemented = [s for s in STAGES if status[s] == "pending"]
    print(f"\nUnimplemented stages (M0 stub — real logic lands in M1+): {unimplemented}")


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
