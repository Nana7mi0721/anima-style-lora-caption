#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 1.5b: Pre-vision character roster resolution via Animadex + cache + local dict."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

try:
    from . import common
except ImportError:
    _SCRIPTS_DIR = Path(__file__).resolve().parent
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    import common

ANIMADEX_SEARCH_URL = "https://animadex.net/api/characters/search"


def to_search_query(raw_tag: str) -> str:
    """Convert raw character tag (e.g. 'katou_megumi_(saekano)') to Animadex search query."""
    s = raw_tag.strip()
    # Strip trailing parenthetical copyright/series qualifier (both underscore and space forms)
    for sep in ("_(", " (", "\\("):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return s.replace("_", " ").strip()


def _norm_match(value: str) -> str:
    """Normalize a character name or query for fuzzy comparison."""
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def load_char_freq(csv_path: Path, min_freq: int) -> dict[str, int]:
    """Load character tags with count >= min_freq from build_tag_table.py char_freq CSV."""
    counts: dict[str, int] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return counts
        tag_col = "tag" if "tag" in reader.fieldnames else reader.fieldnames[0]
        count_col = "count" if "count" in reader.fieldnames else (
            reader.fieldnames[1] if len(reader.fieldnames) > 1 else None
        )
        for row in reader:
            raw = (row.get(tag_col) or "").strip()
            if not raw:
                continue
            try:
                cnt = int(row.get(count_col, 0) or 0) if count_col else 0
            except ValueError:
                cnt = 0
            if cnt >= min_freq:
                counts[raw] = cnt
    return counts


def load_local_dict(path: Path | None) -> set[str]:
    """Load offline tag dictionary from CSV (name/tag column) or JSON (list/dict)."""
    if path is None or not path.exists():
        return set()
    suffix = path.suffix.lower()
    names: set[str] = set()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in data:
                names.add(common.to_danbooru_key(str(k)))
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    val = item.get("name") or item.get("tag") or ""
                else:
                    val = str(item)
                if val:
                    names.add(common.to_danbooru_key(val))
        return names

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header:
            return names
        lowered = [h.strip().lower() for h in header]
        if "name" in lowered:
            idx = lowered.index("name")
        elif "tag" in lowered:
            idx = lowered.index("tag")
        else:
            idx = 0
            # Header might actually be data if single column and not named 'name'/'tag'
            if header[0] and header[0].strip().lower() not in ("name", "tag"):
                names.add(common.to_danbooru_key(header[0].strip()))
        for row in reader:
            if len(row) > idx and row[idx].strip():
                names.add(common.to_danbooru_key(row[idx].strip()))
    return names


class RosterCache:
    """Persistent JSONL or SQLite cache for Animadex character lookups."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.is_sqlite = bool(path and path.suffix.lower() in (".sqlite", ".db"))
        self.data: dict[str, dict[str, Any]] = {}
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.is_sqlite:
                self._init_sqlite()
            else:
                self._load_jsonl()

    def _init_sqlite(self) -> None:
        assert self.path is not None
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS char_cache ("
                "raw_tag TEXT PRIMARY KEY, "
                "payload TEXT NOT NULL, "
                "updated_at REAL NOT NULL)"
            )
            rows = conn.execute("SELECT raw_tag, payload FROM char_cache").fetchall()
            for raw_tag, payload in rows:
                try:
                    self.data[raw_tag] = json.loads(payload)
                except json.JSONDecodeError:
                    continue

    def _load_jsonl(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_tag = rec.get("raw_tag")
            if raw_tag:
                self.data[raw_tag] = rec

    def get(self, raw_tag: str) -> dict[str, Any] | None:
        return self.data.get(raw_tag)

    def put(self, raw_tag: str, entry: dict[str, Any]) -> None:
        if self.path is None:
            return
        payload = dict(entry)
        payload["raw_tag"] = raw_tag
        self.data[raw_tag] = payload
        if self.is_sqlite:
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO char_cache (raw_tag, payload, updated_at) "
                    "VALUES (?, ?, ?)",
                    (raw_tag, json.dumps(payload, ensure_ascii=False), time.time()),
                )
        else:
            lines = [
                json.dumps(v, ensure_ascii=False)
                for _, v in sorted(self.data.items())
            ]
            common.atomic_write_text(self.path, "\n".join(lines) + ("\n" if lines else ""))


def _pick_animadex_match(
    raw_tag: str, query: str, results: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, bool, str]:
    """Select best Animadex result and determine whether match is exact/confirmed."""
    if not results:
        return None, False, "animadex returned 0 results"

    raw_key = common.to_danbooru_key(raw_tag)
    for item in results:
        slug = str(item.get("slug") or "").strip().lower()
        if slug and (slug == raw_tag.lower() or slug == raw_key):
            note = f"exact slug match ({len(results)} results)" if len(results) > 1 else "exact slug match"
            return item, True, note

    norm_q = _norm_match(query)
    name_hits = [
        item for item in results
        if _norm_match(str(item.get("name") or "")) == norm_q
    ]
    if len(name_hits) == 1:
        return name_hits[0], True, "exact name match"
    if len(name_hits) > 1:
        return name_hits[0], False, f"multiple exact name matches ({len(name_hits)}); confirm with user"

    return results[0], False, f"fuzzy match among {len(results)} results; confirm with user"


def _extract_visual_hints(hit: dict[str, Any]) -> list[str]:
    """Extract compact visual hints / copyright context from Animadex hit."""
    hints: list[str] = []
    for key in ("visual_hints", "appearance", "description", "tags"):
        val = hit.get(key)
        if isinstance(val, list):
            hints.extend(str(x).strip() for x in val if str(x).strip())
        elif isinstance(val, str) and val.strip():
            hints.append(val.strip())
    copyright_str = str(hit.get("copyright") or "").strip()
    if copyright_str:
        hints.append(f"series: {copyright_str}")
    return hints


def resolve_character(
    raw_tag: str,
    count: int,
    cache: RosterCache,
    local_dict: set[str],
    *,
    proxy: str | None = None,
    use_curl: bool = False,
    offline: bool = False,
) -> dict[str, Any]:
    """Resolve one character tag via cache -> Animadex -> local dict fallback."""
    cached = cache.get(raw_tag)
    if cached is not None and cached.get("anima_tag"):
        return {
            "count": count,
            "anima_tag": cached["anima_tag"],
            "source": cached.get("source", "cache"),
            "confirmed": bool(cached.get("confirmed", False)),
            "visual_hints": cached.get("visual_hints", []),
            "note": cached.get("note", "from cache"),
        }

    query = to_search_query(raw_tag)
    err_note = ""
    if not offline:
        try:
            resp = common.http_get_json(
                ANIMADEX_SEARCH_URL,
                params={"q": query, "sort": "count", "page": 1},
                proxy=proxy,
                use_curl=use_curl,
            )
            results = resp.get("results", []) if isinstance(resp, dict) else []
            hit, confirmed, note = _pick_animadex_match(raw_tag, query, results)
            if hit is not None:
                trigger_raw = str(hit.get("trigger") or "").strip()
                anima_tag = trigger_raw.split(",")[0].strip() if trigger_raw else query
                entry = {
                    "count": count,
                    "anima_tag": anima_tag or query,
                    "source": "animadex",
                    "confirmed": confirmed,
                    "visual_hints": _extract_visual_hints(hit),
                    "note": note,
                }
                cache.put(raw_tag, entry)
                return entry
            err_note = note
        except Exception as exc:  # noqa: BLE001
            err_note = f"animadex error: {exc}"
    else:
        err_note = "offline mode"

    # Fallback to local dict
    raw_key = common.to_danbooru_key(raw_tag)
    if raw_key in local_dict or raw_tag.lower() in local_dict:
        # Preserve space-form with escaped parentheses if qualified
        anima_guess = raw_tag.replace("_", " ").replace("(", r"\(").replace(")", r"\)")
        if r"\\(" in anima_guess:
            anima_guess = anima_guess.replace(r"\\(", r"\(").replace(r"\\)", r"\)")
        return {
            "count": count,
            "anima_tag": anima_guess,
            "source": "local_dict",
            "confirmed": False,
            "visual_hints": [],
            "note": f"{err_note}; found in local_dict, verify Anima spelling",
        }

    return {
        "count": count,
        "anima_tag": query,
        "source": "fallback",
        "confirmed": False,
        "visual_hints": [],
        "note": f"{err_note}; not in local_dict, using derived query — confirm with user",
    }


def format_roster_markdown(roster: dict[str, dict[str, Any]]) -> str:
    """Format char_roster.json as a concise markdown table for user sign-off."""
    lines = [
        "| Raw Tag | Count | Anima Tag | Source | Confirmed | Note |",
        "|---|---:|---|---|---|---|",
    ]
    for raw_tag, info in roster.items():
        conf = "yes" if info.get("confirmed") else "NO"
        note = str(info.get("note") or "").replace("|", "/")
        lines.append(
            f"| `{raw_tag}` | {info.get('count', 0)} | `{info.get('anima_tag', '')}` "
            f"| {info.get('source', '')} | {conf} | {note} |"
        )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Stage 1.5b: Resolve character tags via Animadex / cache / local dict."
    )
    ap.add_argument("--char-freq", type=Path, default=None, help="Path to char_freq.csv from build_tag_table.py")
    ap.add_argument("--tags", nargs="+", default=None, help="Explicit character tags (comma or space separated)")
    ap.add_argument("--min-freq", type=int, default=4, help="Minimum frequency threshold (default: 4)")
    ap.add_argument("--out", type=Path, default=Path("char_roster.json"), help="Output char_roster.json path")
    ap.add_argument("--cache", type=Path, default=None, help="Persistent JSONL or SQLite cache path")
    ap.add_argument("--local-dict", type=Path, default=None, help="Optional offline CSV/JSON tag dictionary")
    ap.add_argument("--proxy", type=str, default=None, help="HTTP/HTTPS proxy URL")
    ap.add_argument("--use-curl", action="store_true", help="Use curl transport for HTTP calls")
    ap.add_argument("--offline", action="store_true", help="Skip Animadex network calls and use cache/local dict")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.char_freq and not args.tags:
        print("Error: provide --char-freq and/or --tags", file=sys.stderr)
        return 2

    freq_map: dict[str, int] = {}
    if args.char_freq:
        if not args.char_freq.exists():
            print(f"Error: --char-freq not found: {args.char_freq}", file=sys.stderr)
            return 2
        freq_map = load_char_freq(args.char_freq, args.min_freq)

    targets: list[str] = list(freq_map.keys())
    if args.tags:
        for chunk in args.tags:
            for item in chunk.split(","):
                tok = item.strip()
                if tok and tok not in freq_map:
                    targets.append(tok)
                    freq_map.setdefault(tok, 0)

    targets.sort(key=lambda t: (-freq_map.get(t, 0), t))

    cache = RosterCache(args.cache)
    local_dict = load_local_dict(args.local_dict)

    roster: dict[str, dict[str, Any]] = {}
    for raw_tag in targets:
        roster[raw_tag] = resolve_character(
            raw_tag,
            freq_map.get(raw_tag, 0),
            cache,
            local_dict,
            proxy=args.proxy,
            use_curl=args.use_curl,
            offline=args.offline,
        )

    common.atomic_write_json(args.out, roster)
    print(format_roster_markdown(roster))
    unconfirmed = [k for k, v in roster.items() if not v.get("confirmed")]
    print(
        f"\nSaved {len(roster)} characters to {args.out} "
        f"({len(unconfirmed)} unconfirmed requiring user sign-off)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
