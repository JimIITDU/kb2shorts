"""
Assets stage (M5).
For each scene in script.json, resolve a background: cache hit, Pexels
download, or gradient fallback. Every asset gets a license record.
"""

import hashlib
import json
import subprocess
from pathlib import Path

import requests

from pipeline.providers.stock import search_video, StockProviderError

ASSETS_CACHE_DIR = Path("assets_cache")

GRADIENT_COLORS = [
    ("0x1e3a5f", "0x0d1b2a"),
    ("0x3d0d5f", "0x1a0b2e"),
    ("0x0d5f4a", "0x0a2e26"),
    ("0x5f2d0d", "0x2e1608"),
    ("0x0d3d5f", "0x081a2e"),
]


class AssetsError(Exception):
    """Raised when asset resolution fails for a scene with no fallback possible."""


def _cache_key(keyword: str) -> str:
    return hashlib.sha256(keyword.lower().strip().encode("utf-8")).hexdigest()[:16]


def _find_cache_hit(keywords: list[str]) -> Path | None:
    for kw in keywords:
        key = _cache_key(kw)
        candidate = ASSETS_CACHE_DIR / f"{key}.mp4"
        license_path = ASSETS_CACHE_DIR / f"{key}.license.json"
        if candidate.exists() and license_path.exists():
            return candidate
    return None


def _download_and_cache(keyword: str, duration: float) -> Path | None:
    try:
        result = search_video(keyword, min_duration=duration)
    except StockProviderError:
        return None

    if not result:
        return None

    key = _cache_key(keyword)
    video_path = ASSETS_CACHE_DIR / f"{key}.mp4"
    license_path = ASSETS_CACHE_DIR / f"{key}.license.json"

    try:
        resp = requests.get(result["download_url"], timeout=30, stream=True)
        resp.raise_for_status()
        with open(video_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
    except requests.RequestException:
        return None

    license_record = {
        "keyword": keyword,
        "source": result["source"],
        "source_url": result["source_url"],
        "author": result["author"],
        "license": result["license"],
    }
    license_path.write_text(json.dumps(license_record, indent=2), encoding="utf-8")

    return video_path


def _make_gradient(keyword: str, duration: float) -> Path:
    key = _cache_key(keyword)
    video_path = ASSETS_CACHE_DIR / f"{key}_gradient.mp4"
    license_path = ASSETS_CACHE_DIR / f"{key}_gradient.license.json"

    if video_path.exists():
        return video_path

    color_index = int(key, 16) % len(GRADIENT_COLORS)
    c1, c2 = GRADIENT_COLORS[color_index]

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"gradients=s=1080x1920:c0={c1}:c1={c2}:d={max(duration, 3)}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(video_path),
            ],
            capture_output=True, timeout=30, check=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else ""
        raise AssetsError(f"Gradient fallback generation failed: {stderr[-500:]}")

    license_record = {
        "keyword": keyword,
        "source": "generated",
        "source_url": None,
        "author": None,
        "license": "N/A — locally generated gradient, no third-party asset.",
    }
    license_path.write_text(json.dumps(license_record, indent=2), encoding="utf-8")

    return video_path


def run_assets(job_dir: Path, config: dict) -> dict:
    script_path = job_dir / "script.json"
    if not script_path.exists():
        raise AssetsError("script.json not found — run script generation stage first.")

    script = json.loads(script_path.read_text(encoding="utf-8"))
    ASSETS_CACHE_DIR.mkdir(exist_ok=True)

    scene_assets = []
    stats = {"cache_hits": 0, "pexels_hits": 0, "gradient_fallbacks": 0}

    for i, scene in enumerate(script["scenes"]):
        keywords = scene.get("keywords", [])
        duration = scene.get("est_seconds", 5.0)

        resolved_path = None

        cache_hit = _find_cache_hit(keywords)
        if cache_hit:
            resolved_path = cache_hit
            stats["cache_hits"] += 1
        else:
            for kw in keywords:
                downloaded = _download_and_cache(kw, duration)
                if downloaded:
                    resolved_path = downloaded
                    stats["pexels_hits"] += 1
                    break

        if not resolved_path:
            fallback_keyword = keywords[0] if keywords else f"scene_{i}"
            resolved_path = _make_gradient(fallback_keyword, duration)
            stats["gradient_fallbacks"] += 1

        license_path = Path(str(resolved_path).replace(".mp4", ".license.json"))

        scene_assets.append({
            "scene_index": i,
            "role": scene.get("role"),
            "asset_path": str(resolved_path),
            "license_path": str(license_path) if license_path.exists() else None,
        })

    assets_meta = {"scenes": scene_assets, "stats": stats}
    assets_path = job_dir / "assets.json"
    assets_path.write_text(json.dumps(assets_meta, indent=2, ensure_ascii=False), encoding="utf-8")

    missing_license = [s for s in scene_assets if s["license_path"] is None]
    if missing_license:
        raise AssetsError(
            f"{len(missing_license)} scene(s) resolved to an asset with no license record."
        )

    return stats