"""Stage 1.5: Build tag_table.csv, gap-driven review_todo.csv, char_freq.csv, and update per_image.json."""

from __future__ import annotations

import argparse
import collections
import csv
import io
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    atomic_write_text,
    infer_slot,
    is_negative_token,
    iter_images,
    load_per_image,
    load_rules,
    norm_key,
    normalize_token,
    to_danbooru_key,
    upsert_per_image,
)

_DISAMBIG_NON_CHAR = {
    "(animal)",
    "(object)",
    "(symbol)",
    "(planet)",
    "(sky)",
    "(costume)",
    "(medium)",
    "(meme)",
    "(food)",
    "(vehicle)",
}
_NSFW_SIGNAL_RE = re.compile(
    r"\b(?:nude|completely nude|nipples|pussy|penis|anus|cum|sex|vaginal|anal|"
    r"fellatio|paizuri|handjob|masturbation|spread legs|breasts out|one breast out)\b"
)


def _split_caption_tokens(text: str) -> list[str]:
    s = text.strip()
    if s.startswith(","):
        s = s.lstrip(",").strip()
    return [part.strip() for part in s.split(",") if part.strip()]


def _load_metadata_csv(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    records: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            stem = (
                row.get("stem")
                or row.get("id")
                or (Path(row["filename"]).stem if row.get("filename") else "")
                or (Path(row["image"]).stem if row.get("image") else "")
            )
            stem = str(stem).strip()
            if not stem:
                continue
            general_raw = row.get("tags_full") or row.get("tags") or row.get("general") or ""
            char_raw = row.get("character") or row.get("tags_character") or ""
            artist_raw = row.get("artist") or row.get("tags_artist") or ""
            copy_raw = row.get("copyright") or row.get("tags_copyright") or ""
            sep = "," if "," in general_raw else " "
            records[stem] = {
                "general": [t.strip() for t in general_raw.split(sep) if t.strip()],
                "character": [t.strip() for t in re.split(r"[, ]+", char_raw) if t.strip()],
                "artist": [t.strip() for t in re.split(r"[, ]+", artist_raw) if t.strip()],
                "copyright": [t.strip() for t in re.split(r"[, ]+", copy_raw) if t.strip()],
            }
    return records


def _is_char_candidate(raw_tag: str, meta_chars: set[str]) -> bool:
    tl = raw_tag.strip().lower()
    if to_danbooru_key(tl) in meta_chars or norm_key(tl) in meta_chars:
        return True
    if r"\(" in raw_tag or "(" in raw_tag:
        for suffix in _DISAMBIG_NON_CHAR:
            if tl.endswith(suffix):
                return False
        return True
    return False


def _detect_conflicts(tokens: list[str], rules: dict[str, Any]) -> list[str]:
    slots_cfg = rules.get("slots", {})
    groups = slots_cfg.get("conflict_groups", {})
    exemptions = slots_cfg.get("conflict_exemptions", {})
    tok_set = set(tokens)

    conflicts: list[str] = []
    if "solo" in tok_set and "solo focus" in tok_set:
        conflicts.append("solo+solo_focus")

    multi_person = set(exemptions.get("multi_person_tokens", []))
    is_multi = bool(tok_set & multi_person) or any(
        re.match(r"^(?:[2-9]|\d{2,})\+?(?:girl|boy|other)s?$", t) for t in tok_set
    )
    if is_multi:
        return conflicts

    hair_exempt = set(exemptions.get("hair_color_exempt_tokens", []))
    eye_exempt = set(exemptions.get("eye_color_exempt_tokens", []))

    for group_name, candidates in groups.items():
        if group_name == "hair_color" and (tok_set & hair_exempt):
            continue
        if group_name == "eye_color" and (tok_set & eye_exempt):
            continue
        hits = [c for c in candidates if c in tok_set]
        if len(hits) > 1:
            conflicts.append(f"{group_name}:{'/'.join(hits)}")

    return conflicts


def _choose_preset(
    has_a: bool,
    missing_slots: list[str],
    tag_count: int,
    min_tags: int,
    tokens: list[str],
) -> str:
    joined = " ".join(tokens)
    is_nsfw = bool(_NSFW_SIGNAL_RE.search(joined))
    if has_a and tag_count >= min_tags:
        missing_set = set(missing_slots)
        if missing_set <= {"5", "6"} and "1" not in missing_set and "3" not in missing_set:
            return "composition_only"
        if missing_set <= {"1", "5", "6"} and "2" not in missing_set and "3" not in missing_set:
            return "people_and_composition"
    return "full_nsfw" if is_nsfw else "full_sfw"


def _csv_text(fieldnames: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def run_build_tag_table(
    dataset: Path,
    out_table: Path,
    out_todo: Path,
    out_chars: Path,
    per_image_path: Path | None = None,
    metadata_csv: Path | None = None,
    drafts_dir: Path | None = None,
    rules_dir: Path | None = None,
    extra_rules: Path | None = None,
    min_tags: int = 20,
    min_char_freq: int = 4,
    trigger: str | None = None,
) -> dict[str, Any]:
    """Scan dataset + drafts + metadata to emit tag_table, review_todo, char_freq, and per_image."""
    rules = load_rules(rules_dir=rules_dir, extra_rules_path=extra_rules)
    images = iter_images(dataset, recursive=True)
    existing_per = load_per_image(per_image_path) if per_image_path else {}
    meta_records = _load_metadata_csv(metadata_csv)
    trigger_norm = trigger.strip().lower() if trigger else ""

    tag_counts: collections.Counter[str] = collections.Counter()
    char_counts: collections.Counter[str] = collections.Counter()
    meta_artists: set[str] = set()
    meta_copyrights: set[str] = set()
    meta_chars: set[str] = set()
    for m in meta_records.values():
        meta_artists.update(to_danbooru_key(x) for x in m.get("artist", []))
        meta_copyrights.update(to_danbooru_key(x) for x in m.get("copyright", []))
        meta_chars.update(to_danbooru_key(x) for x in m.get("character", []))

    todo_rows: list[dict[str, Any]] = []
    per_image_updates: dict[str, dict[str, Any]] = {}

    for img_path in images:
        stem = img_path.stem
        try:
            rel_img = str(img_path.relative_to(dataset)).replace("\\", "/")
        except ValueError:
            rel_img = img_path.name

        draft_path = (drafts_dir / f"{stem}.txt") if drafts_dir else img_path.with_suffix(".txt")
        draft_raw_tokens: list[str] = []
        if draft_path.is_file():
            draft_raw_tokens = _split_caption_tokens(draft_path.read_text(encoding="utf-8", errors="replace"))

        meta = meta_records.get(stem, {})
        existing_rec = existing_per.get(stem, {})
        a_tags = meta.get("general") or existing_rec.get("tags_a") or []
        a_chars = meta.get("character") or existing_rec.get("characters_a") or []
        has_a = bool(a_tags or a_chars)

        combined_raw: list[str] = []
        seen_raw: set[str] = set()
        for raw in list(a_chars) + list(a_tags) + draft_raw_tokens:
            rl = raw.strip()
            if not rl:
                continue
            if trigger_norm and rl.lower() == trigger_norm:
                continue
            if rl not in seen_raw:
                seen_raw.add(rl)
                combined_raw.append(rl)

        normalized_tokens: list[str] = []
        present_slots: set[str] = set()

        for raw in combined_raw:
            key = norm_key(raw)
            db_key = to_danbooru_key(raw)
            tag_counts[key] += 1

            if _is_char_candidate(raw, meta_chars):
                char_counts[key] += 1
                present_slots.add("1")
                continue

            if db_key in meta_artists or db_key in meta_copyrights:
                continue

            normed = normalize_token(raw, rules)
            for nt in normed:
                if nt not in normalized_tokens:
                    normalized_tokens.append(nt)
                    present_slots.add(infer_slot(nt, rules))

        missing_slots = [s for s in ("1", "2", "3", "4", "5", "6") if s not in present_slots]
        conflicts = _detect_conflicts(normalized_tokens, rules)
        tag_count = len(normalized_tokens)
        preset = _choose_preset(has_a, missing_slots, tag_count, min_tags, normalized_tokens)

        if conflicts or not has_a or tag_count < min_tags or "1" in missing_slots:
            priority = "high"
        elif "3" in missing_slots or "6" in missing_slots:
            priority = "medium"
        elif missing_slots:
            priority = "low"
        else:
            priority = "skip"

        todo_rows.append(
            {
                "stem": stem,
                "image": rel_img,
                "has_a_source": "true" if has_a else "false",
                "tag_count": tag_count,
                "missing_slots": ";".join(missing_slots),
                "conflicts": ";".join(conflicts),
                "priority": priority,
                "preset": preset,
            }
        )

        rec_update: dict[str, Any] = {
            "image": rel_img,
            "has_a_source": has_a,
            "draft_tags": draft_raw_tokens,
            "missing_slots": missing_slots,
            "preset": preset,
        }
        if a_tags:
            rec_update["tags_a"] = a_tags
        if a_chars:
            rec_update["characters_a"] = a_chars
        per_image_updates[stem] = rec_update

    vfix = rules.get("vision_fix", {})
    aliases = vfix.get("aliases", {})
    color_downgrade = vfix.get("color_downgrade", {})
    multi_split = vfix.get("multi_split", {})
    dep_drop = {norm_key(x) for x in vfix.get("deprecated_drop", [])}
    banned_all = rules.get("banned", {}).get("banned_all", set())
    score_year_re = rules.get("banned", {}).get("score_year_re", [])

    table_rows: list[dict[str, Any]] = []
    for tag, count in tag_counts.most_common():
        db_key = to_danbooru_key(tag)
        if db_key in meta_artists:
            action, target, slot, note = "DELETE", "", "", "artist tag covered by trigger"
        elif db_key in meta_copyrights:
            action, target, slot, note = "DELETE", "", "", "copyright/IP tag excluded"
        elif tag in char_counts:
            action, target, slot, note = "CHARACTER", "", "1", "character tag candidate"
        elif tag in rules.get("neg_keep", set()):
            action, target, slot, note = "KEEP", "", infer_slot(tag, rules), "verified negative-prefix Danbooru tag"
        elif is_negative_token(tag, rules):
            action, target, slot, note = "DELETE", "", "", "disallowed negative token"
        elif tag in rules.get("whitelist", set()):
            action, target, slot, note = "KEEP", "", infer_slot(tag, rules), "approved visual descriptor whitelist"
        elif tag in multi_split or db_key in multi_split:
            parts = multi_split.get(tag) or multi_split.get(db_key) or []
            action = "NORMALIZE"
            target = ";".join(parts)
            slot = infer_slot(parts[0], rules) if parts else "2"
            note = "deprecated multi-split tag"
        elif tag in aliases or db_key in aliases:
            tgt = aliases.get(tag) or aliases.get(db_key) or ""
            action, target, slot, note = "NORMALIZE", tgt, infer_slot(tgt, rules), "alias to canonical tag"
        elif tag in color_downgrade:
            tgt = color_downgrade[tag]
            action, target, slot, note = "NORMALIZE", tgt, infer_slot(tgt, rules), "compound color downgrade"
        elif tag in dep_drop or db_key in dep_drop:
            action, target, slot, note = "DELETE", "", "", "deprecated without replacement"
        elif tag in banned_all or any(rx.match(tag) or rx.match(db_key) for rx in score_year_re):
            action, target, slot, note = "DELETE", "", "", "banned meta/quality/text/score tag"
        else:
            action, target, slot, note = "KEEP", "", infer_slot(tag, rules), "visual fact tag"

        table_rows.append(
            {
                "tag": tag,
                "count": count,
                "action": action,
                "target": target,
                "slot": slot,
                "note": note,
            }
        )

    char_rows = [
        {
            "tag": tag,
            "count": count,
            "meets_threshold": "true" if count >= min_char_freq else "false",
        }
        for tag, count in char_counts.most_common()
    ]

    atomic_write_text(
        out_table,
        _csv_text(["tag", "count", "action", "target", "slot", "note"], table_rows),
    )
    atomic_write_text(
        out_todo,
        _csv_text(
            ["stem", "image", "has_a_source", "tag_count", "missing_slots", "conflicts", "priority", "preset"],
            todo_rows,
        ),
    )
    atomic_write_text(
        out_chars,
        _csv_text(["tag", "count", "meets_threshold"], char_rows),
    )
    if per_image_path is not None:
        upsert_per_image(per_image_path, per_image_updates)

    return {
        "images": len(images),
        "unique_tags": len(table_rows),
        "characters": len(char_rows),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build tag_table.csv, review_todo.csv, and char_freq.csv.")
    ap.add_argument("--dataset", required=True, type=Path, help="Dataset image directory")
    ap.add_argument("--out-table", required=True, type=Path, help="Output tag_table.csv")
    ap.add_argument("--out-todo", required=True, type=Path, help="Output review_todo.csv")
    ap.add_argument("--out-chars", required=True, type=Path, help="Output char_freq.csv")
    ap.add_argument("--per-image", type=Path, default=None, help="Incremental per_image.json path")
    ap.add_argument("--metadata-csv", type=Path, default=None, help="Optional booru metadata CSV")
    ap.add_argument("--drafts-dir", type=Path, default=None, help="Optional draft captions directory")
    ap.add_argument("--rules-dir", type=Path, default=None, help="Override rules directory")
    ap.add_argument("--extra-rules", type=Path, default=None, help="Optional dataset wash_rules.json")
    ap.add_argument("--min-tags", type=int, default=20, help="Minimum expected tag count excluding trigger")
    ap.add_argument("--min-char-freq", type=int, default=4, help="Character frequency threshold (default: 4)")
    ap.add_argument("--trigger", default=None, help="Optional trigger token to exclude from counts")
    args = ap.parse_args(argv)

    stats = run_build_tag_table(
        dataset=args.dataset,
        out_table=args.out_table,
        out_todo=args.out_todo,
        out_chars=args.out_chars,
        per_image_path=args.per_image,
        metadata_csv=args.metadata_csv,
        drafts_dir=args.drafts_dir,
        rules_dir=args.rules_dir,
        extra_rules=args.extra_rules,
        min_tags=args.min_tags,
        min_char_freq=args.min_char_freq,
        trigger=args.trigger,
    )
    print(
        f"Tag table built: images={stats['images']} unique_tags={stats['unique_tags']} "
        f"characters={stats['characters']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
