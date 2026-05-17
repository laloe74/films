"""
Sync watched movies from NeoDB to films.toml.

Two modes:
  auto  (default): fetch latest 20 marks, append new only, no deletion.
  full:            fetch ALL marks, add new + remove deleted.

Usage:
    NEOB_API_TOKEN=xxx python scripts/sync_films.py          # auto
    NEOB_API_TOKEN=xxx SYNC_MODE=full python scripts/sync_films.py

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
        "name": item.get("display_title") or item.get("title", ""),
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


def sync_full(token: str):
    """Full mode: fetch ALL marks, add new + remove deleted."""
    print("=== Full sync (compare all) ===")
    marks = fetch_marks(token)
    print(f"  NeoDB: {len(marks)} marks")

    neo_urls = {norm_url(m["item"]["url"]) for m in marks if m.get("item", {}).get("url")}
    existing = load_existing(TOML_PATH)

    local_neo = [e for e in existing if norm_url(e.get("url", "")).startswith("/movie/")]
    local_manual = [e for e in existing if not norm_url(e.get("url", "")).startswith("/movie/")]
    local_neo_urls = {norm_url(e["url"]) for e in local_neo}

    keep_urls = local_neo_urls & neo_urls
    removed_urls = local_neo_urls - neo_urls
    new_urls = neo_urls - local_neo_urls

    kept = [e for e in local_neo if norm_url(e["url"]) in keep_urls]
    removed = [e for e in local_neo if norm_url(e["url"]) in removed_urls]
    new_marks = [m for m in marks if norm_url(m["item"]["url"]) in new_urls]

    print(f"  Keep: {len(kept)}  Remove: {len(removed)}  Add: {len(new_marks)}  Manual: {len(local_manual)}")

    if removed:
        print("Removing:")
        for e in removed:
            print(f"  - [{e['date']}] {e['name']}")

    max_idx = max((e.get("index", 0) for e in existing), default=0)
    new_entries = []
    for i, m in enumerate(new_marks):
        entry = mark_to_entry(m, max_idx + 1 + i)
        new_entries.append(entry)
        print(f"  + [{entry['date']}] {entry['name']} ({stars_str(entry['score'])})")

    if not removed and not new_entries:
        print("Already up to date.")
        update_index_timestamp(INDEX_PATH)
        return

    all_entries = local_manual + kept + new_entries
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

    mode = os.environ.get("SYNC_MODE", "auto")
    if mode == "full":
        sync_full(token)
    else:
        sync_auto(token)


if __name__ == "__main__":
    main()
