"""
Package stage (M8).
Assembles the final deliverable: output/<job_id>/ with video.mp4,
metadata.json, title.txt, description.txt, license_manifest.json,
qa_report.json.

Refuses to run if the QA gate didn't pass — packaging a failing job is
exactly the mistake this stage exists to prevent.
"""

import json
import shutil
from pathlib import Path

OUTPUT_ROOT = Path("output")


class PackageError(Exception):
    """Raised when packaging cannot proceed."""


def _collect_attributions(assets_path: Path) -> list[str]:
    if not assets_path.exists():
        return []
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    lines = []
    for scene in assets.get("scenes", []):
        license_path = scene.get("license_path")
        if not license_path:
            continue
        lp = Path(license_path)
        if not lp.exists():
            continue
        record = json.loads(lp.read_text(encoding="utf-8"))
        if record.get("source") == "generated":
            continue  # locally generated gradient — no third-party attribution needed
        author = record.get("author") or "Unknown"
        source = record.get("source") or "Unknown source"
        source_url = record.get("source_url") or ""
        lines.append(f"- {author} via {source} ({source_url})".strip())

    seen = set()
    unique_lines = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)
    return unique_lines


def _build_license_manifest(assets_path: Path) -> dict:
    if not assets_path.exists():
        return {"assets": []}
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    manifest_entries = []
    for scene in assets.get("scenes", []):
        license_path = scene.get("license_path")
        record = None
        if license_path and Path(license_path).exists():
            record = json.loads(Path(license_path).read_text(encoding="utf-8"))
        manifest_entries.append({
            "scene_index": scene.get("scene_index"),
            "asset_path": scene.get("asset_path"),
            "license_record": record,
        })
    return {"assets": manifest_entries}


def run_package(job_dir: Path, config: dict) -> dict:
    qa_report_path = job_dir / "qa_report.json"
    video_path = job_dir / "video.mp4"
    script_path = job_dir / "script.json"
    assets_path = job_dir / "assets.json"

    if not qa_report_path.exists():
        raise PackageError("qa_report.json not found — run the QA stage first.")
    qa_report = json.loads(qa_report_path.read_text(encoding="utf-8"))
    if not qa_report.get("passed"):
        failed = [c["name"] for c in qa_report.get("checks", []) if not c.get("passed")]
        raise PackageError(f"QA did not pass ({failed}) — refusing to package a failing job.")

    if not video_path.exists():
        raise PackageError("video.mp4 not found.")
    if not script_path.exists():
        raise PackageError("script.json not found.")

    script = json.loads(script_path.read_text(encoding="utf-8"))

    job_id = job_dir.name
    out_dir = OUTPUT_ROOT / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(video_path, out_dir / "video.mp4")

    (out_dir / "title.txt").write_text(script["title"], encoding="utf-8")

    description_lines = [script["description"]]
    attribution_lines = _collect_attributions(assets_path)
    if attribution_lines:
        description_lines.append("")
        description_lines.append("Footage credits:")
        description_lines.extend(attribution_lines)
    (out_dir / "description.txt").write_text(
        "\n".join(description_lines), encoding="utf-8"
    )

    metadata = {
        "job_id": job_id,
        "title": script["title"],
        "description": script["description"],
        "tags": script.get("tags", []),
        "scene_count": len(script.get("scenes", [])),
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    license_manifest = _build_license_manifest(assets_path)
    (out_dir / "license_manifest.json").write_text(
        json.dumps(license_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    shutil.copy2(qa_report_path, out_dir / "qa_report.json")

    return {
        "output_dir": str(out_dir),
        "files": sorted(p.name for p in out_dir.iterdir()),
    }