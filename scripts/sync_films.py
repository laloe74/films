"""
Sync watched movies from NeoDB to films.toml.

Fetch latest marks from NeoDB shelf and append new entries only (no deletion).

Usage:
    NEOB_API_TOKEN=xxx python scripts/sync_films.py

Requires: Python 3.11+ (stdlib tomllib)
"""

import json
import math
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

NEOB_API = "https://neodb.social/api"
NEOB_BASE = "https://neodb.social"
TOML_PATH = Path("content/films.toml")
INDEX_PATH = Path("content/_index.md")
PAGE_SIZE = 50
CST = timezone(timedelta(hours=8))


def _is_chinese(s: str) -> bool:
    """Check if a string contains Chinese characters (CJK Unified Ideographs)."""
    return bool(re.search(r'[一-鿿㐀-䶿]', s))


def _normalize_lang(lang: str) -> str:
    """Normalize a language tag for comparison (e.g. zh_CN -> zh-cn)."""
    return lang.strip().lower().replace("_", "-")


def _title_rank(lang: str) -> int:
    """Rank a language tag for title selection (lower is better).

    0: Simplified Chinese, Mainland China (zh-CN, zh-CNhans)
    1: Simplified Chinese, other tags (zh-Hans, zh-SG, zh-MY)
    2: Chinese, any other tag (zh, zh-Hant, zh-TW, zh-HK, ...)
    3: Non-Chinese
    """
    key = _normalize_lang(lang)
    if key in ("zh-cn", "zh-cnhans", "zh-cn-hans"):
        return 0
    if key.startswith("zh-hans") or key in ("zh-sg", "zh-my"):
        return 1
    if key.startswith("zh"):
        return 2
    return 3


def _pick_chinese_title(item: dict) -> str:
    """Pick the best Chinese title from NeoDB item fields.

    Priority:
        1. Simplified Chinese, Mainland China (zh-CN)
        2. Simplified Chinese, other tags (zh-Hans)
        3. Chinese, any other tag (zh, zh-Hant, zh-TW, zh-HK, ...)
        4. title / display_title containing Chinese characters
        5. title

    NeoDB API has deprecated display_title and removed alt_title.
    Chinese names are now in localized_title as [{lang: "zh-Hans", text: "..."}].
    """
    localized = item.get("localized_title", [])
    best_text = None
    best_rank = None
    for loc in localized:
        lang = loc.get("lang", "") or ""
        text = loc.get("text", "")
        if not text:
            continue
        rank = _title_rank(lang)
        if rank == 3:
            continue
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_text = text

    if best_text:
        return best_text

    title = item.get("title", "")
    display = item.get("display_title", "")

    # Prefer Chinese in title
    if title and _is_chinese(title):
        return title
    # Then Chinese in display_title (deprecated but still present)
    if display and _is_chinese(display):
        return display
    # Fall back to title
    return title


def norm_url(url: str) -> str:
    """Normalize URL to just the path for comparison."""
    if url.startswith(NEOB_BASE):
        return url[len(NEOB_BASE):]
    return url


def full_url(path: str) -> str:
    """Ensure URL has full https://neodb.social prefix."""
    if path.startswith("http"):
        return path
    return NEOB_BASE + path


def fetch_marks(token: str, page_size: int = PAGE_SIZE, max_pages: int | None = None) -> list[dict]:
    """Fetch 'complete' movie marks from NeoDB shelf.

    max_pages=None means fetch all pages; max_pages=1 fetches only first page.
    """
    all_marks = []
    page = 1
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "films-sync/1.0",
    }
    while True:
        url = f"{NEOB_API}/me/shelf/complete?category=movie&page={page}&page_size={page_size}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            print(f"HTTP {e.code} on page {page}: {body}", file=sys.stderr)
            raise
        all_marks.extend(data["data"])
        if page >= data["pages"] or (max_pages and page >= max_pages):
            break
        page += 1
        if max_pages is None:
            time.sleep(0.5)
    return all_marks


def neo_score_to_stars(grade: int | None) -> int:
    if grade is None:
        return 0
    return math.ceil(grade / 2)


def load_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("movies", [])


def mark_to_entry(mark: dict, index: int) -> dict:
    item = mark.get("item", {})
    return {
        "index": index,
        "name": _pick_chinese_title(item),
        "date": mark.get("created_time", "")[:10],
        "score": neo_score_to_stars(mark.get("rating_grade")),
        "url": full_url(item.get("url", "")),
    }


def format_toml(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        lines.append("[[movies]]")
        lines.append(f'index = {entry["index"]}')
        lines.append(f'name = "{entry["name"]}"')
        lines.append(f'date = "{entry["date"]}"')
        lines.append(f'score = {entry["score"]}')
        lines.append(f'url = "{entry["url"]}"')
        lines.append("")
    return "\n".join(lines)


def update_index_timestamp(path: Path):
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    text = path.read_text(encoding="utf-8")
    if 'updated_at =' in text:
        text = re.sub(r'updated_at = ".*"', f'updated_at = "{now}"', text)
    else:
        text = text.replace('[extra]\n', f'[extra]\nupdated_at = "{now}"\n')
    path.write_text(text, encoding="utf-8")
    print(f"Updated timestamp: {now}")


def stars_str(score: int) -> str:
    if score == 0:
        return "---"
    return "★" * score + "☆" * (5 - score)


def sync_auto(token: str):
    """Auto mode: fetch first 20 marks, append new only."""
    print("=== Auto sync (latest 20 only) ===")
    marks = fetch_marks(token, page_size=20, max_pages=1)
    print(f"  NeoDB: {len(marks)} marks")

    existing = load_existing(TOML_PATH)
    local_urls = {norm_url(e["url"]) for e in existing if e["url"]}
    max_idx = max((e.get("index", 0) for e in existing), default=0)

    new_marks = [m for m in marks if norm_url(m["item"]["url"]) not in local_urls]
    if not new_marks:
        print("  No new movies.")
        update_index_timestamp(INDEX_PATH)
        return

    new_entries = []
    for i, m in enumerate(new_marks):
        entry = mark_to_entry(m, max_idx + 1 + i)
        new_entries.append(entry)
        print(f"  + [{entry['date']}] {entry['name']} ({stars_str(entry['score'])})")

    all_entries = existing + new_entries
    all_entries.sort(key=lambda e: (e.get("date", ""), e.get("index", 0)), reverse=True)
    toml_text = format_toml(all_entries)
    TOML_PATH.write_text(toml_text, encoding="utf-8")
    print(f"Wrote {len(all_entries)} movies to {TOML_PATH}")
    update_index_timestamp(INDEX_PATH)


def main():
    token = os.environ.get("NEOB_API_TOKEN")
    if not token:
        print("Error: NEOB_API_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    sync_auto(token)


if __name__ == "__main__":
    main()
