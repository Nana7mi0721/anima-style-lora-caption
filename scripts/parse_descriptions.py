"""Stage 2.5: Parse strict JSON or legacy FIELD: description files into supplement.json."""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    IMAGE_EXTS,
    atomic_write_json,
    atomic_write_text,
    infer_slot,
    iter_images,
    load_rules,
    norm_key,
    normalize_token,
    to_danbooru_key,
)

_REFUSE_RE = re.compile(
    r"(?:拒绝|无法|不能|抱歉|不予|\brefus|\bcannot\b|\bcan't\b|\bunable\b|\bsorry\b|as an ai)",
    re.IGNORECASE,
)
_FIELD_LINE_RE = re.compile(r"^\s*#?\s*([A-Za-z][A-Za-z_/ ]{1,24}?)\s*[:：]\s*(.*)$")
_CODE_FENCE_RE = re.compile(r"```(?:json|text|txt)?\s*|\s*```", re.IGNORECASE)
_KAOMOJI_TOKENS = (":3", "^_^", ">_<", ";_;", "o_o", "0_0", "uwu", "owo", "^q^")
_COMPACT_PAREN_KEEP = {"(animal)", "(object)", "(symbol)", "(planet)", "(sky)", "(costume)", "(medium)"}


def _load_tag_table(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.is_file():
        return {}
    table: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            tag = row.get("tag", "").strip()
            if tag:
                table[norm_key(tag)] = {
                    "action": row.get("action", "").strip().upper(),
                    "target": row.get("target", "").strip(),
                    "slot": row.get("slot", "").strip(),
                }
    return table


def _load_valid_dict(path: Path | None) -> set[str] | None:
    if path is None or not path.is_file():
        return None
    valid: set[str] = set()
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            valid.update(to_danbooru_key(k) for k in data)
        elif isinstance(data, list):
            valid.update(to_danbooru_key(str(x)) for x in data)
    elif suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                name = row.get("name") or row.get("tag") or ""
                if name:
                    valid.add(to_danbooru_key(name))
    else:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                valid.add(to_danbooru_key(s))
    return valid


def _strip_comment_parens(text: str) -> str:
    """Remove parenthetical commentary before comma splitting while keeping escaped/disambig parens."""
    # Protect escaped parens temporarily
    protected = text.replace(r"\(", "\x00LP\x00").replace(r"\)", "\x00RP\x00")
    out: list[str] = []
    i = 0
    n = len(protected)
    while i < n:
        ch = protected[i]
        if ch in "(（":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if protected[j] in "(（":
                    depth += 1
                elif protected[j] in ")）":
                    depth -= 1
                j += 1
            if depth == 0:
                inner = protected[i:j]
                if inner.lower() in _COMPACT_PAREN_KEEP:
                    out.append(inner)
                else:
                    # Strip parenthetical comment like "(with man behind, obscured)"
                    out.append(" ")
                i = j
                continue
            # Unmatched opening paren: drop remainder of comment fragment safely
            break
        out.append(ch)
        i += 1
    res = "".join(out).replace("\x00LP\x00", r"\(").replace("\x00RP\x00", r"\)")
    return res


def _split_legacy_fragments(raw_val: str) -> list[str]:
    """Split legacy field text safely while preserving kaomoji like :3 and ;_;."""
    text = _strip_comment_parens(raw_val)
    placeholders: dict[str, str] = {}
    for idx, km in enumerate(_KAOMOJI_TOKENS):
        if km in text:
            ph = f"__KAOMOJI_{idx}__"
            placeholders[ph] = km
            text = text.replace(km, ph)

    fragments: list[str] = []
    for chunk in re.split(r"[,，;；、|]+", text):
        for part in re.split(r"\s+\+\s+", chunk):
            p = part.strip()
            for ph, km in placeholders.items():
                p = p.replace(ph, km)
            if p:
                fragments.append(p)
    return fragments


def _try_parse_json_payload(raw_text: str) -> dict[str, Any] | None:
    stripped = _CODE_FENCE_RE.sub("", raw_text).strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # Attempt to extract JSON object embedded in text
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(stripped[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def _parse_legacy_blocks(raw_text: str, known_fields: set[str]) -> dict[str, str]:
    cleaned = _CODE_FENCE_RE.sub("", raw_text)
    blocks: dict[str, list[str]] = {}
    cur: str | None = None
    for line in cleaned.splitlines():
        m = _FIELD_LINE_RE.match(line)
        if m:
            key = m.group(1).strip().upper()
            if key in known_fields:
                cur = key
                blocks.setdefault(cur, []).append(m.group(2).strip())
                continue
        if cur is not None:
            blocks[cur].append(line.strip())
    return {k: " ".join(v).strip() for k, v in blocks.items()}


def _is_registered(
    token: str,
    rules: dict[str, Any],
    tag_table: dict[str, dict[str, str]],
    valid_dict: set[str] | None,
) -> bool:
    if token in tag_table:
        return True
    if token in rules.get("whitelist", set()) or token in rules.get("neg_keep", set()):
        return True
    slots_cfg = rules.get("slots", {})
    for key in ("slot1_people", "pose_tokens", "composition_tokens", "watermark_tokens", "protected_tokens"):
        if token in slots_cfg.get(key, []):
            return True
    vfix = rules.get("vision_fix", {})
    if token in set(vfix.get("aliases", {}).values()) or token in set(vfix.get("color_downgrade", {}).values()):
        return True
    if valid_dict is not None and to_danbooru_key(token) in valid_dict:
        return True
    return False


def _cleanup_crops(crop_root: Path | None) -> int:
    if crop_root is None or not crop_root.exists():
        return 0
    removed = 0
    for entry in list(crop_root.iterdir()):
        if entry.is_dir():
            removed += sum(1 for p in entry.rglob("*") if p.is_file())
            shutil.rmtree(entry, ignore_errors=True)
        elif entry.is_file():
            entry.unlink(missing_ok=True)
            removed += 1
    return removed


def _has_thumb(thumbs_dir: Path, stem: str) -> bool:
    for ext in IMAGE_EXTS:
        if (thumbs_dir / f"{stem}{ext}").is_file():
            return True
    return False


def _find_desc_file(desc_dir: Path, stem: str) -> Path | None:
    json_path = desc_dir / f"{stem}.json"
    if json_path.is_file():
        return json_path
    txt_path = desc_dir / f"{stem}.txt"
    if txt_path.is_file():
        return txt_path
    return None


def parse_single_stem(
    stem: str,
    desc_path: Path | None,
    rules: dict[str, Any],
    tag_table: dict[str, dict[str, str]],
    valid_dict: set[str] | None,
    thumbs_dir: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None, list[tuple[str, str, str]]]:
    """Parse one stem description into structured slots or return failure reason."""
    if thumbs_dir is not None and not _has_thumb(thumbs_dir, stem):
        return None, "no_vision_input", []
    if desc_path is None or not desc_path.is_file():
        return None, "no_description_file", []

    raw_text = desc_path.read_text(encoding="utf-8", errors="replace")
    if not raw_text.strip():
        return None, "subagent_empty", []

    field_to_slot: dict[str, str | None] = rules.get("slots", {}).get("field_to_slot", {})
    known_fields = {k.upper() for k in field_to_slot}
    pose_tokens = set(rules.get("slots", {}).get("pose_tokens", []))
    tag_table_slots = {k: v["slot"] for k, v in tag_table.items() if v.get("slot")}

    json_obj = _try_parse_json_payload(raw_text)
    fields_data: dict[str, list[str]] = {}
    raw_fields: dict[str, Any] = {}

    if json_obj is not None:
        matched_any = False
        for k, val in json_obj.items():
            ku = str(k).strip().upper()
            if ku not in known_fields:
                continue
            matched_any = True
            raw_fields[ku] = val
            if isinstance(val, list):
                fields_data[ku] = [str(x) for x in val]
            elif val is not None:
                fields_data[ku] = _split_legacy_fragments(str(val))
        if not matched_any:
            if _REFUSE_RE.search(raw_text):
                return None, "refused", []
            return None, "no_block", []
    else:
        blocks = _parse_legacy_blocks(raw_text, known_fields)
        if not blocks:
            if _REFUSE_RE.search(raw_text):
                return None, "refused", []
            return None, "no_block", []
        for k, val in blocks.items():
            raw_fields[k] = val
            if field_to_slot.get(k) is None:
                fields_data[k] = [val] if val else []
            else:
                fields_data[k] = _split_legacy_fragments(val)

    slots: dict[str, list[str]] = collections.defaultdict(list)
    seen_global: set[str] = set()
    review_items: list[tuple[str, str, str]] = []
    metadata: dict[str, str] = {}

    for field, items in fields_data.items():
        default_slot = field_to_slot.get(field)
        if default_slot is None:
            joined = ", ".join(x.strip() for x in items if str(x).strip())
            if joined and joined.lower() not in {"none", "n/a", "na", "-"}:
                metadata[field.lower()] = joined
            continue

        if field == "CHARACTER":
            char_raw = ", ".join(str(x).strip() for x in items if str(x).strip())
            if char_raw and char_raw.lower() not in {"none", "n/a", "na", "-"}:
                metadata["character"] = char_raw

        for raw_item in items:
            raw_str = str(raw_item).strip()
            if field == "CHARACTER" and (
                raw_str.lower().startswith("other:")
                or raw_str.lower() in {"other", "uncertain"}
            ):
                continue
            # JSON list items may still contain commentary parens if LLM slipped
            item_clean = _strip_comment_parens(raw_str) if json_obj is not None else raw_str
            for tok in normalize_token(item_clean, rules, valid_dict=valid_dict):
                tbl_entry = tag_table.get(tok)
                if tbl_entry:
                    if tbl_entry["action"] == "DELETE":
                        continue
                    if tbl_entry["action"] == "NORMALIZE" and tbl_entry["target"]:
                        tok = norm_key(tbl_entry["target"])

                # Field slot takes precedence for structured extraction, except re-slotting
                # pose tokens misplaced into COMPOSITION -> slot 3, or explicit tag_table slots.
                target_slot = default_slot
                if field == "COMPOSITION" and tok in pose_tokens:
                    target_slot = "3"
                elif field not in ("POSE", "COMPOSITION", "WATERMARK", "TEXT", "LIGHTING"):
                    target_slot = infer_slot(tok, rules, tag_table_slots=tag_table_slots, default_slot=default_slot)

                if tok in seen_global:
                    continue
                seen_global.add(tok)
                slots[target_slot].append(tok)

                if not _is_registered(tok, rules, tag_table, valid_dict):
                    review_items.append((field, tok, target_slot))

    if not any(slots.values()):
        return None, "parse_empty", review_items

    entry: dict[str, Any] = {
        "slots": {k: v for k, v in sorted(slots.items()) if v},
        "watermark": {
            "present": bool(slots.get("7")),
            "tokens": list(slots.get("7", [])),
        },
        "character": metadata.get("character", ""),
        "corrections": metadata.get("corrections", ""),
        "note": metadata.get("note", ""),
        "raw": raw_fields,
    }
    return entry, None, review_items


def run_parse_descriptions(
    desc_dir: Path,
    out_json: Path,
    out_failed: Path,
    out_review: Path,
    dataset: Path | None = None,
    tag_table_path: Path | None = None,
    rules_dir: Path | None = None,
    extra_rules: Path | None = None,
    crop_root: Path | None = None,
    thumbs_dir: Path | None = None,
    valid_dict_path: Path | None = None,
) -> dict[str, Any]:
    """Parse all descriptions into supplement.json, failed ledger, and review list."""
    rules = load_rules(rules_dir=rules_dir, extra_rules_path=extra_rules)
    tag_table = _load_tag_table(tag_table_path)
    valid_dict = _load_valid_dict(valid_dict_path)

    stems: list[str]
    if dataset is not None and dataset.is_dir():
        stems = sorted({p.stem for p in iter_images(dataset, recursive=True)})
    else:
        stems = sorted(
            {
                p.stem
                for p in desc_dir.iterdir()
                if p.is_file() and p.suffix.lower() in {".json", ".txt"}
            }
        ) if desc_dir.is_dir() else []

    parsed: dict[str, Any] = {}
    failed: list[tuple[str, str]] = []
    review_lines: list[str] = []
    unregistered_counts: collections.Counter[str] = collections.Counter()

    for stem in stems:
        desc_path = _find_desc_file(desc_dir, stem) if desc_dir.is_dir() else None
        entry, reason, review_items = parse_single_stem(
            stem=stem,
            desc_path=desc_path,
            rules=rules,
            tag_table=tag_table,
            valid_dict=valid_dict,
            thumbs_dir=thumbs_dir,
        )
        if reason is not None or entry is None:
            failed.append((stem, reason or "parse_empty"))
            continue
        parsed[stem] = entry
        for field, tok, slot in review_items:
            unregistered_counts[tok] += 1
            review_lines.append(f"{stem}\t{field}\t{tok}\t{slot}")

    atomic_write_json(out_json, parsed)

    failed_text = "".join(f"{stem}\t{reason}\n" for stem, reason in failed)
    atomic_write_text(out_failed, failed_text)

    review_header = [
        "# Unregistered tokens assigned to field default slot (verify before final merge)",
        *[f"{cnt}\t{tok}" for tok, cnt in unregistered_counts.most_common()],
        "",
        "# Per-image occurrences: stem<TAB>field<TAB>token<TAB>slot",
        *review_lines,
    ]
    atomic_write_text(out_review, "\n".join(review_header).rstrip() + "\n")

    cleaned_crops = _cleanup_crops(crop_root)

    return {
        "total": len(stems),
        "parsed": len(parsed),
        "failed": len(failed),
        "unregistered_tokens": len(unregistered_counts),
        "cleaned_crops": cleaned_crops,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Parse JSON or legacy vision descriptions into supplement.json.")
    ap.add_argument("--desc-dir", required=True, type=Path, help="Directory with <stem>.json or <stem>.txt")
    ap.add_argument("--dataset", type=Path, default=None, help="Optional dataset directory to detect missing files")
    ap.add_argument("--out-json", required=True, type=Path, help="Output supplement JSON path")
    ap.add_argument("--out-failed", required=True, type=Path, help="Output vision_failed.txt path")
    ap.add_argument("--out-review", required=True, type=Path, help="Output vision_review.txt path")
    ap.add_argument("--tag-table", type=Path, default=None, help="Optional tag_table.csv path")
    ap.add_argument("--rules-dir", type=Path, default=None, help="Override rules directory")
    ap.add_argument("--extra-rules", type=Path, default=None, help="Optional dataset wash_rules.json")
    ap.add_argument("--crop-root", type=Path, default=None, help="Optional temp crop directory to clean up")
    ap.add_argument("--thumbs-dir", type=Path, default=None, help="Optional thumbnails directory to verify input")
    ap.add_argument("--valid-dict", type=Path, default=None, help="Optional offline Danbooru dictionary path")
    args = ap.parse_args(argv)

    stats = run_parse_descriptions(
        desc_dir=args.desc_dir,
        out_json=args.out_json,
        out_failed=args.out_failed,
        out_review=args.out_review,
        dataset=args.dataset,
        tag_table_path=args.tag_table,
        rules_dir=args.rules_dir,
        extra_rules=args.extra_rules,
        crop_root=args.crop_root,
        thumbs_dir=args.thumbs_dir,
        valid_dict_path=args.valid_dict,
    )
    print(
        f"Parsed {stats['parsed']}/{stats['total']} descriptions "
        f"(failed={stats['failed']}, unregistered={stats['unregistered_tokens']}, "
        f"cleaned_crops={stats['cleaned_crops']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
