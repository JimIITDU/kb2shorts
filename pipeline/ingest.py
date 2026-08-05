"""
Ingest stage (M1).
Turns raw input (a local file path OR a URL) into a clean article.txt
plus a source.json metadata file inside the job directory.
"""

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import trafilatura

MIN_WORDS = 80
WARN_WORDS = 3000


class IngestError(Exception):
    """Raised when ingest cannot produce usable article text."""


def is_url(input_str: str) -> bool:
    parsed = urlparse(input_str)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n\n".join(lines)


def clean_from_file(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    return normalize_text(raw)


def clean_from_url(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise IngestError(f"Failed to fetch URL: {e}")

    extracted = trafilatura.extract(resp.text, favor_recall=True)
    if not extracted or not extracted.strip():
        raise IngestError(
            "Could not extract article content from this URL "
            "(page may not be a readable article, or may require JS rendering)."
        )
    return normalize_text(extracted)


def run_ingest(input_str: str, job_dir: Path) -> dict:
    job_dir.mkdir(parents=True, exist_ok=True)

    if is_url(input_str):
        text = clean_from_url(input_str)
        source_type = "url"
        source_ref = input_str
    else:
        path = Path(input_str)
        if not path.exists():
            raise IngestError(f"Input file not found: {input_str}")
        text = clean_from_file(path)
        source_type = "file"
        source_ref = str(path)

    word_count = len(text.split())

    if word_count < MIN_WORDS:
        raise IngestError(
            f"Article too thin to make a video: {word_count} words "
            f"(minimum {MIN_WORDS})."
        )

    warning = None
    if word_count > WARN_WORDS:
        warning = (
            f"Article is {word_count} words — summarization in M2 "
            f"will be aggressive to fit the 40-60s target."
        )

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    article_path = job_dir / "article.txt"
    article_path.write_text(text, encoding="utf-8")

    source_meta = {
        "source_type": source_type,
        "source_ref": source_ref,
        "content_sha256": content_hash,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "word_count": word_count,
        "warning": warning,
    }
    source_path = job_dir / "source.json"
    source_path.write_text(
        json.dumps(source_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return source_meta