#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 4: Offline Gate 1 + Online 3-state Gate 2 with persistent SQLite cache & alias suggestions."""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import statistics
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

DANBOORU_TAGS_URL = "https://danbooru.donmai.us/tags.json"
DANBOORU_ALIASES_URL = "https://danbooru.donmai.us/tag_aliases.json"
CACHE_TTL_SECONDS = 30 * 86400

SCORE_UNDERSCORE_RE = re.compile(r"^score_\d+(_up)?$")
ALWAYS_MULTI_PEOPLE = {
    "2girls", "3girls", "4girls", "5girls", "6+girls", "multiple girls",
    "2boys", "3boys", "4boys", "5boys", "6+boys", "multiple boys",
    "hetero", "yuri", "yaoi", "bisexual", "group sex", "threesome",
    "ffm threesome", "mmf threesome", "orgy",
}


def _is_multi_person(token_set: set[str], rules: dict[str, Any]) -> bool:
    exemptions = rules.get("slots", {}).get("conflict_exemptions", {})
    multi_set = set(exemptions.get("multi_person_tokens", [])) | ALWAYS_MULTI_PEOPLE
    if token_set & multi_set:
        return True
    if (
        ("1girl" in token_set and "1boy" in token_set)
        or ("1girl" in token_set and "1other" in token_set)
        or ("1boy" in token_set and "1other" in token_set)
    ):
        return True
    return False


def _is_banned(tok: str, rules: dict[str, Any]) -> bool:
    banned = rules.get("banned", {})
    banned_all = set(banned.get("banned_all", []))
    score_res = banned.get("score_year_re", [])
    low = tok.strip().lower()
    if low in banned_all or common.to_danbooru_key(low) in banned_all:
        return True
    for rx in score_res:
        if isinstance(rx, str):
            if re.match(rx, low):
                return True
        elif rx.match(low):
            return True
    return False


def _load_watermark_ledger(path: Path | None, rules: dict[str, Any]) -> dict[str, bool]:
    """Load expected watermark presence per stem from supplement or ledger JSON."""
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    wm_tokens = set(rules.get("slots", {}).get("watermark_tokens", ["watermark", "signature", "logo"]))
    ledger: dict[str, bool] = {}
    for stem, val in data.items():
        if stem.startswith("_"):
            continue
        if isinstance(val, bool):
            ledger[stem] = val
        elif isinstance(val, list):
            ledger[stem] = bool(val)
        elif isinstance(val, dict):
            if "watermark_present" in val:
                ledger[stem] = bool(val["watermark_present"])
            elif "has_watermark" in val:
                ledger[stem] = bool(val["has_watermark"])
            elif isinstance(val.get("watermark"), dict) and "present" in val["watermark"]:
                ledger[stem] = bool(val["watermark"]["present"])
            elif "slots" in val and isinstance(val["slots"], dict):
                s7 = val["slots"].get("7", [])
                all_toks = {t for toks in val["slots"].values() for t in toks}
                ledger[stem] = bool(s7) or bool(all_toks & wm_tokens)
    return ledger


def _check_conflicts(tokens: list[str], rules: dict[str, Any]) -> list[str]:
    """Check hair_color, eye_color, hair_length conflict groups with the 3 mandatory exemptions."""
    tok_set = set(tokens)
    slots_cfg = rules.get("slots", {})
    conflict_groups: dict[str, list[str]] = slots_cfg.get("conflict_groups", {})
    exemptions: dict[str, list[str]] = slots_cfg.get("conflict_exemptions", {})
    neg_keep = set(rules.get("neg_keep", []))

    # Exemption (a): skip on multi-person images
    if _is_multi_person(tok_set, rules):
        return []

    hair_exempt = set(exemptions.get(
        "hair_color_exempt_tokens",
        ["multicolored hair", "two-tone hair", "gradient hair", "colored inner hair", "streaked hair"],
    ))
    eye_exempt = set(exemptions.get(
        "eye_color_exempt_tokens",
        ["heterochromia", "gradient eyes", "multicolored eyes"],
    ))

    issues: list[str] = []
    for group_name, group_tokens in conflict_groups.items():
        # Exemption (b): multicolored hair / heterochromia exemptions
        if group_name == "hair_color" and (tok_set & hair_exempt):
            continue
        if group_name == "eye_color" and (tok_set & eye_exempt):
            continue
        # Exemption (c): neg_keep tokens like `no panties` / `no bra` are NEVER in a conflict group
        group_set = set(group_tokens) - neg_keep - {"no panties", "no bra"}
        hits = [t for t in tokens if t in group_set]
        if len(set(hits)) > 1:
            issues.append(f"{group_name}: {', '.join(sorted(set(hits)))}")
    return issues


def validate_offline(
    captions_dir: Path,
    *,
    dataset_dir: Path | None,
    trigger: str | None,
    rules: dict[str, Any],
    min_tags: int,
    max_tags: int,
    watermark_ledger_path: Path | None = None,
) -> tuple[dict[str, list[Any]], dict[str, list[Any]], list[int], collections.Counter[str]]:
    """Run Gate 1 offline checks on all .txt captions in captions_dir."""
    errors: dict[str, list[Any]] = collections.defaultdict(list)
    warnings: dict[str, list[Any]] = collections.defaultdict(list)
    counts: list[int] = []
    token_freq: collections.Counter[str] = collections.Counter()

    txt_files = sorted(captions_dir.glob("*.txt"))
    got_stems = {p.stem for p in txt_files}

    if dataset_dir and dataset_dir.is_dir():
        expect_stems = {p.stem for p in common.iter_images(dataset_dir, recursive=True)}
        missing = sorted(expect_stems - got_stems)
        extra = sorted(got_stems - expect_stems)
        if missing:
            errors["missing_captions"].extend(missing)
        if extra:
            errors["orphan_captions"].extend(extra)

    ledger = _load_watermark_ledger(watermark_ledger_path, rules)
    wm_tokens = set(rules.get("slots", {}).get("watermark_tokens", [
        "watermark", "blue watermark", "red watermark", "signature",
        "artist name", "logo", "speech bubble", "sound effects",
    ]))
    trigger_low = trigger.strip().lower() if trigger else None

    for txt_path in txt_files:
        stem = txt_path.stem
        raw = txt_path.read_bytes()

        if raw.startswith(b"\xef\xbb\xbf"):
            errors["bom"].append(stem)
            raw = raw[3:]
        if b"\r" in raw:
            errors["crlf"].append(stem)

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            errors["invalid_utf8"].append(stem)
            continue

        if text.endswith("\n") or text.endswith("\r"):
            errors["trailing_newline"].append(stem)
        stripped = text.strip()
        if "\n" in stripped:
            errors["multiline"].append(stem)
            stripped = stripped.splitlines()[0].strip()
        if not stripped:
            errors["empty"].append(stem)
            counts.append(0)
            continue
        if stripped.startswith(","):
            errors["leading_comma"].append(stem)
        if stripped.endswith((".", ",", ";", ":")):
            errors["trailing_punctuation"].append(stem)
        if stripped != stripped.lower():
            errors["uppercase"].append(stem)
        if "  " in stripped:
            errors["double_space"].append(stem)

        tokens = [t.strip() for t in stripped.split(",") if t.strip()]

        # Trigger check
        if trigger_low:
            if not tokens or tokens[0] != trigger_low:
                errors["trigger_not_first"].append({"stem": stem, "first": tokens[0] if tokens else None})
            at_indices = [i for i, t in enumerate(tokens) if "@" in t]
            if at_indices != [0]:
                errors["stray_at_symbol"].append({"stem": stem, "indices": at_indices})
        else:
            at_indices = [i for i, t in enumerate(tokens) if "@" in t and i != 0]
            if at_indices:
                errors["stray_at_symbol"].append({"stem": stem, "indices": at_indices})

        # Body tokens strictly exclude leading @trigger
        if tokens and ((trigger_low and tokens[0] == trigger_low) or (not trigger_low and tokens[0].startswith("@"))):
            body_tokens = tokens[1:]
        else:
            body_tokens = tokens

        # Space-form check (score_* exception)
        underscore_toks = [
            t for t in body_tokens
            if "_" in t and not SCORE_UNDERSCORE_RE.match(t)
        ]
        if underscore_toks:
            errors["underscore_form"].append({"stem": stem, "tokens": underscore_toks})

        # Duplicates
        dups = sorted(t for t, c in collections.Counter(tokens).items() if c > 1)
        if dups:
            errors["duplicate_tokens"].append({"stem": stem, "tokens": dups})

        # Tag count strictly excluding @trigger
        tag_count = len(body_tokens)
        counts.append(tag_count)
        if tag_count < min_tags:
            warnings["under_min_tags"].append({"stem": stem, "count": tag_count})
        if tag_count > max_tags:
            warnings["over_max_tags"].append({"stem": stem, "count": tag_count})

        # BANNED & invalid negatives
        banned_hits = sorted({t for t in body_tokens if _is_banned(t, rules)})
        if banned_hits:
            errors["banned_tokens"].append({"stem": stem, "tokens": banned_hits})

        neg_hits = sorted({t for t in body_tokens if common.is_negative_token(t, rules)})
        if neg_hits:
            errors["invalid_negative_tokens"].append({"stem": stem, "tokens": neg_hits})

        # People contradictions
        tok_set = set(body_tokens)
        if "solo" in tok_set and "solo focus" in tok_set:
            errors["people_contradiction"].append({"stem": stem, "detail": "solo + solo focus"})
        elif "solo" in tok_set and _is_multi_person(tok_set, rules):
            errors["people_contradiction"].append({"stem": stem, "detail": "solo + multi-person token"})

        # Conflict groups with 3 exemptions
        conflicts = _check_conflicts(body_tokens, rules)
        if conflicts:
            errors["conflict_groups"].append({"stem": stem, "conflicts": conflicts})

        # Watermark ledger consistency
        if stem in ledger:
            expected_wm = ledger[stem]
            has_wm = bool(tok_set & wm_tokens)
            if expected_wm != has_wm:
                errors["watermark_mismatch"].append({
                    "stem": stem,
                    "expected": expected_wm,
                    "actual": has_wm,
                })

        for t in body_tokens:
            token_freq[t] += 1

    return dict(errors), dict(warnings), counts, token_freq


class DanbooruTagCache:
    """Persistent SQLite cache for Danbooru tag lookups."""

    def __init__(self, path: Path | None, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self.path = path
        self.ttl = ttl_seconds
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS danbooru_tags ("
                    "name TEXT PRIMARY KEY, "
                    "status TEXT NOT NULL, "
                    "post_count INTEGER NOT NULL, "
                    "is_deprecated INTEGER NOT NULL, "
                    "category INTEGER, "
                    "reason TEXT, "
                    "checked_at REAL NOT NULL)"
                )

    def get_many(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        if self.path is None or not keys:
            return {}
        now = time.time()
        found: dict[str, dict[str, Any]] = {}
        with sqlite3.connect(self.path) as conn:
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT name, status, post_count, is_deprecated, category, reason, checked_at "
                f"FROM danbooru_tags WHERE name IN ({placeholders})",
                keys,
            ).fetchall()
        for name, status, pc, dep, cat, reason, checked_at in rows:
            if status in ("VALID", "INVALID") and (now - checked_at) < self.ttl:
                found[name] = {
                    "status": status,
                    "post_count": pc,
                    "is_deprecated": bool(dep),
                    "category": cat,
                    "reason": reason or "",
                    "from_cache": True,
                }
        return found

    def put_many(self, records: dict[str, dict[str, Any]]) -> None:
        if self.path is None or not records:
            return
        now = time.time()
        rows = [
            (
                name,
                rec["status"],
                int(rec.get("post_count", 0) or 0),
                1 if rec.get("is_deprecated") else 0,
                rec.get("category"),
                rec.get("reason", ""),
                now,
            )
            for name, rec in records.items()
            if rec.get("status") in ("VALID", "INVALID")
        ]
        if not rows:
            return
        with sqlite3.connect(self.path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO danbooru_tags "
                "(name, status, post_count, is_deprecated, category, reason, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )


def _resolve_auth(auth_arg: str | None) -> tuple[str, str] | None:
    if auth_arg and ":" in auth_arg:
        user, key = auth_arg.split(":", 1)
        return (user, key)
    env_user = os.environ.get("DANBOORU_LOGIN")
    env_key = os.environ.get("DANBOORU_API_KEY")
    if env_user and env_key:
        return (env_user, env_key)
    return None


def validate_online(
    unique_tokens: set[str],
    *,
    rules: dict[str, Any],
    cache_path: Path | None,
    batch_size: int,
    proxy: str | None,
    use_curl: bool,
    auth: tuple[str, str] | None,
    suggest_aliases: bool,
) -> dict[str, Any]:
    """Run Gate 2 online 3-state validation (VALID / INVALID / UNVERIFIED)."""
    whitelist = set(rules.get("whitelist", []))
    # Skip @trigger, escaped-paren character tags, and whitelist descriptors
    to_check = sorted(
        t for t in unique_tokens
        if not t.startswith("@") and r"\(" not in t and t not in whitelist
    )
    key_to_tags: dict[str, list[str]] = collections.defaultdict(list)
    for tok in to_check:
        key_to_tags[common.to_danbooru_key(tok)].append(tok)

    all_keys = sorted(key_to_tags.keys())
    cache = DanbooruTagCache(cache_path)
    cached_hits = cache.get_many(all_keys)

    key_results: dict[str, dict[str, Any]] = dict(cached_hits)
    uncached_keys = [k for k in all_keys if k not in cached_hits]
    batch_errors: list[str] = []

    for i in range(0, len(uncached_keys), batch_size):
        chunk = uncached_keys[i : i + batch_size]
        params = {
            "search[name_comma]": ",".join(chunk),
            "limit": len(chunk) * 2,
            "only": "name,category,post_count,is_deprecated",
        }
        try:
            resp = common.http_get_json(
                DANBOORU_TAGS_URL,
                params=params,
                proxy=proxy,
                auth=auth,
                use_curl=use_curl,
            )
            if not isinstance(resp, list):
                raise ValueError(f"Unexpected Danbooru response type: {type(resp).__name__}")
            by_name = {str(item["name"]): item for item in resp if isinstance(item, dict) and "name" in item}
            new_records: dict[str, dict[str, Any]] = {}
            for key in chunk:
                item = by_name.get(key)
                if item is None:
                    rec = {
                        "status": "INVALID",
                        "post_count": 0,
                        "is_deprecated": False,
                        "category": None,
                        "reason": "absent",
                        "from_cache": False,
                    }
                elif item.get("is_deprecated"):
                    rec = {
                        "status": "INVALID",
                        "post_count": int(item.get("post_count", 0) or 0),
                        "is_deprecated": True,
                        "category": item.get("category"),
                        "reason": "deprecated",
                        "from_cache": False,
                    }
                elif int(item.get("post_count", 0) or 0) <= 0:
                    rec = {
                        "status": "INVALID",
                        "post_count": 0,
                        "is_deprecated": False,
                        "category": item.get("category"),
                        "reason": "zero_posts",
                        "from_cache": False,
                    }
                else:
                    rec = {
                        "status": "VALID",
                        "post_count": int(item["post_count"]),
                        "is_deprecated": False,
                        "category": item.get("category"),
                        "reason": "",
                        "from_cache": False,
                    }
                key_results[key] = rec
                new_records[key] = rec
            cache.put_many(new_records)
        except Exception as exc:  # noqa: BLE001
            # Network/HTTP failure MUST yield UNVERIFIED, never ABSENT/INVALID
            batch_errors.append(str(exc))
            for key in chunk:
                key_results[key] = {
                    "status": "UNVERIFIED",
                    "post_count": None,
                    "is_deprecated": None,
                    "category": None,
                    "reason": f"network_error: {exc}",
                    "from_cache": False,
                }

    tag_results: dict[str, dict[str, Any]] = {}
    for key, toks in key_to_tags.items():
        res = key_results[key]
        for tok in toks:
            tag_results[tok] = dict(res, danbooru_key=key)

    # Alias suggestions for INVALID tags
    alias_suggestions: dict[str, dict[str, Any]] = {}
    if suggest_aliases:
        semantic_traps = rules.get("vision_fix", {}).get("semantic_traps", {})
        invalid_tags = [t for t, r in sorted(tag_results.items()) if r["status"] == "INVALID"]
        for tok in invalid_tags:
            key = common.to_danbooru_key(tok)
            trap_info = semantic_traps.get(tok) or semantic_traps.get(key)
            consequent: str | None = None
            try:
                aliases = common.http_get_json(
                    DANBOORU_ALIASES_URL,
                    params={"search[antecedent_name]": key, "search[status]": "active"},
                    proxy=proxy,
                    auth=auth,
                    use_curl=use_curl,
                )
                if isinstance(aliases, list):
                    for a in aliases:
                        if isinstance(a, dict) and a.get("consequent_name"):
                            consequent = str(a["consequent_name"]).replace("_", " ")
                            break
            except Exception:  # noqa: BLE001
                consequent = None

            suggestion: dict[str, Any] = {
                "consequent": consequent,
                "semantic_trap": bool(trap_info),
            }
            if trap_info:
                suggestion["trap_detail"] = trap_info
            alias_suggestions[tok] = suggestion

    return {
        "results": tag_results,
        "alias_suggestions": alias_suggestions,
        "batch_errors": batch_errors,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Stage 4: Validate captions offline (Gate 1) and optionally online (Gate 2)."
    )
    ap.add_argument("--captions-dir", type=Path, required=True, help="Directory of .txt captions to validate")
    ap.add_argument("--dataset", type=Path, default=None, help="Dataset images directory (for 1:1 pairing)")
    ap.add_argument("--trigger", type=str, default=None, help="Expected leading @trigger")
    ap.add_argument("--rules-dir", type=Path, default=None, help="Optional custom rules directory")
    ap.add_argument("--extra-rules", type=Path, default=None, help="Optional extra rules JSON file")
    ap.add_argument("--min-tags", type=int, default=20, help="Minimum tags excluding @trigger (default: 20)")
    ap.add_argument("--max-tags", type=int, default=45, help="Maximum tags excluding @trigger (default: 45)")
    ap.add_argument("--watermark-ledger", type=Path, default=None, help="Optional watermark ledger/supplement JSON")
    ap.add_argument("--online", action="store_true", help="Enable Gate 2 Danbooru online 3-state validation")
    ap.add_argument("--cache", type=Path, default=None, help="Persistent SQLite cache path for Danbooru tags")
    ap.add_argument("--batch-size", type=int, default=120, help="Danbooru batch size (default: 120)")
    ap.add_argument("--proxy", type=str, default=None, help="HTTP/HTTPS proxy URL")
    ap.add_argument("--use-curl", action="store_true", help="Use curl transport for HTTP calls")
    ap.add_argument("--auth", type=str, default=None, help="Danbooru auth user:api_key")
    ap.add_argument("--suggest-aliases", action="store_true", help="Query active aliases & flag semantic traps for INVALID tags")
    ap.add_argument("--out-report", type=Path, default=None, help="Output JSON validation report path")
    ap.add_argument("--strict", action="store_true", help="Exit non-zero on errors OR online_unverified > 0")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    rules = common.load_rules(rules_dir=args.rules_dir, extra_rules_path=args.extra_rules)

    errors, warnings, counts, token_freq = validate_offline(
        args.captions_dir,
        dataset_dir=args.dataset,
        trigger=args.trigger,
        rules=rules,
        min_tags=args.min_tags,
        max_tags=args.max_tags,
        watermark_ledger_path=args.watermark_ledger,
    )

    online_data: dict[str, Any] = {"results": {}, "alias_suggestions": {}, "batch_errors": []}
    if args.online:
        online_data = validate_online(
            set(token_freq.keys()),
            rules=rules,
            cache_path=args.cache,
            batch_size=args.batch_size,
            proxy=args.proxy,
            use_curl=args.use_curl,
            auth=_resolve_auth(args.auth),
            suggest_aliases=args.suggest_aliases,
        )

    online_results = online_data["results"]
    n_valid = sum(1 for r in online_results.values() if r["status"] == "VALID")
    n_invalid = sum(1 for r in online_results.values() if r["status"] == "INVALID")
    n_unverified = sum(1 for r in online_results.values() if r["status"] == "UNVERIFIED")

    pairing_errors = len(errors.get("missing_captions", [])) + len(errors.get("orphan_captions", []))
    total_offline_errors = sum(len(v) for v in errors.values())

    report = {
        "summary": {
            "total_captions": len(counts),
            "total_offline_errors": total_offline_errors,
            "pairing_errors": pairing_errors,
            "total_warnings": sum(len(v) for v in warnings.values()),
            "tag_count_mean": round(statistics.mean(counts), 2) if counts else 0.0,
            "tag_count_median": statistics.median(counts) if counts else 0,
            "tag_count_min": min(counts) if counts else 0,
            "tag_count_max": max(counts) if counts else 0,
            "online_checked": len(online_results),
            "online_valid": n_valid,
            "online_invalid": n_invalid,
            "online_unverified": n_unverified,
        },
        "errors": errors,
        "warnings": warnings,
        "online": online_data,
    }

    if args.out_report:
        common.atomic_write_json(args.out_report, report)

    print(
        f"Validation: {len(counts)} captions | offline_errors={total_offline_errors} "
        f"| warnings={report['summary']['total_warnings']} "
        f"| online(valid={n_valid}, invalid={n_invalid}, unverified={n_unverified})"
    )

    has_blocking = total_offline_errors > 0 or n_invalid > 0 or (args.strict and n_unverified > 0)
    return 1 if has_blocking else 0


if __name__ == "__main__":
    sys.exit(main())
