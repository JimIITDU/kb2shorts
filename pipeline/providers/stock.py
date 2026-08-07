"""
Stock footage provider (M5).
Wraps the Pexels API. Only this file talks to Pexels directly.
Respects rate limits (max 1 request/second) with basic backoff on 429.
"""

import os
import time

import requests

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"

_last_request_time = 0.0


class StockProviderError(Exception):
    """Raised when the stock footage API call fails."""


def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_request_time = time.time()


def search_video(keyword: str, min_duration: float) -> dict | None:
    """
    Searches Pexels for a portrait-orientation video matching `keyword`,
    at least `min_duration` seconds long. Returns a dict with download URL
    and license/attribution info, or None if nothing suitable is found.
    """
    api_key = os.getenv("PEXELS_API_KEY")
    if not api_key:
        raise StockProviderError("PEXELS_API_KEY not set in environment (.env)")

    headers = {"Authorization": api_key}
    params = {"query": keyword, "orientation": "portrait", "per_page": 5}

    max_retries = 3
    for attempt in range(max_retries):
        _rate_limit()
        try:
            resp = requests.get(PEXELS_SEARCH_URL, headers=headers, params=params, timeout=15)
        except requests.RequestException as e:
            raise StockProviderError(f"Pexels request failed: {e}")

        if resp.status_code == 429:
            wait = 2 ** attempt
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            raise StockProviderError(f"Pexels API returned {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        videos = data.get("videos", [])

        for video in videos:
            if video.get("duration", 0) < min_duration:
                continue
            files = sorted(
                video.get("video_files", []),
                key=lambda f: f.get("width", 0),
                reverse=True,
            )
            best_file = None
            for f in files:
                if f.get("width", 0) and f.get("height", 0):
                    if f["height"] >= f["width"]:
                        best_file = f
                        break
            if not best_file and files:
                best_file = files[0]

            if best_file:
                return {
                    "download_url": best_file["link"],
                    "source": "pexels",
                    "source_url": video.get("url"),
                    "author": video.get("user", {}).get("name", "unknown"),
                    "license": "Pexels License (free to use, no attribution required)",
                }

        return None

    raise StockProviderError("Pexels API rate limit exceeded after retries.")