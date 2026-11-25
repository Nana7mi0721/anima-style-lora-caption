#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 3: Merge 3-source / 2-source / vision-only tags, fold parents, apply overrides, and tiered-trim."""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

try:
    from . import common
except ImportError:
    _SCRIPTS_DIR = Path(__file__).resolve().parent
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    import common

SLOT_ORDER_DEFAULT = ["1", "2", "3", "4", "5", "6", "7"]

PEOPLE_COUNT_RE = re.compile(r"^(\d+\+?(girl|boy|other)s?|multiple (girls|boys|others))$")
RELATION_TOKENS = {
    "solo", "solo focus", "hetero", "yuri", "yaoi", "bisexual",
    "group sex", "threesome", "ffm threesome", "mmf threesome", "orgy",
    "faceless male", "faceless female", "male focus", "female focus",
    "multiple views", "group picture",
}
DEFAULT_HAIR_COLORS = {
    "black hair", "blonde hair", "brown hair", "red hair", "blue hair",
    "green hair", "pink hair", "purple hair", "white hair", "grey hair",
    "orange hair", "aqua hair", "silver hair",
}
DEFAULT_MULTI_PERSON_TOKENS = {
    "2girls", "3girls", "4girls", "5girls", "6+girls", "multiple girls",
    "2boys", "3boys", "4boys", "5boys", "6+boys", "multiple boys",
    "hetero", "yuri", "yaoi", "bisexual", "group sex", "threesome",
    "ffm threesome", "mmf threesome", "orgy", "solo focus",
}
DEFAULT_PROTECTED_HEADS = {
    "ass", "eyes", "sex", "pussy", "cum", "water", "solo", "focus",
    "hair", "face", "mouth", "day", "night", "sun", "light", "body", "star",
}
DEFAULT_POSE_ORDER = [
    "standing", "sitting", "lying", "on back", "on side", "on stomach", "kneeling",
    "squatting", "crouching", "all fours", "reclining", "floating", "walking",
    "running", "jumping", "leaning forward", "bent over", "straddling",
    "legs up", "spread legs", "legs apart", "legs together", "leg up",
    "arms up", "arms behind back", "arm behind back", "hands up", "hand up",
    "arm up", "outstretched arm", "spread arms", "crossed arms", "head tilt",
    "hand on own face", "hand on own cheek", "hand on own hip", "hand on own chest",
    "holding", "hugging", "carrying", "crossed legs", "arms behind head",
    "sex", "vaginal", "anal", "girl on top", "upright straddle",
    "cowgirl position", "reverse cowgirl position", "missionary", "mating press",
    "doggystyle", "paizuri", "handjob", "fellatio", "masturbation", "fingering",
    "pussy", "penis", "anus", "ass", "cum", "cum in pussy", "cum on breasts",
    "cumdrip", "pussy juice", "saliva", "sweat", "wet", "after sex",
]
MOUTH_EXCLUSIVE = ("open mouth", "closed mouth")
MARK_EXCLUSIVE = ("logo", "watermark")


def _load_json_if_exists(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_tag_table(path: Path | None) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Load tag_table.csv -> (entries_by_key, tag_table_slots)."""
    entries: dict[str, dict[str, str]] = {}
    slot_map: dict[str, str] = {}
    if path is None or not path.exists():
        return entries, slot_map
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("tag") or "").strip()
            if not raw:
                continue
            key = common.norm_key(raw)
            entries[key] = row
            entries[raw.lower()] = row
            slot = (row.get("slot") or "").strip()
            if slot:
                slot_map[key] = slot
                slot_map[raw.lower()] = slot
    return entries, slot_map


def load_char_roster(path: Path | None) -> tuple[dict[str, str], set[str]]:
    """Load char_roster.json -> (raw_or_key -> anima_tag, set of anima character tags)."""
    mapping: dict[str, str] = {}
    char_tags: set[str] = set()
    data = _load_json_if_exists(path)
    if not isinstance(data, dict):
        return mapping, char_tags
    for raw_tag, info in data.items():
        if isinstance(info, dict):
            anima = str(info.get("anima_tag") or "").strip()
        else:
            anima = str(info).strip()
        if anima:
            mapping[raw_tag.lower()] = anima
            mapping[common.norm_key(raw_tag)] = anima
            mapping[common.to_danbooru_key(raw_tag)] = anima
            char_tags.add(anima.lower())
    return mapping, char_tags


def load_supplements(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load supplement JSON file or directory of round*.json shards."""
    merged: dict[str, dict[str, Any]] = {}
    if path is None or not path.exists():
        return merged
    if path.is_dir():
        files = sorted(
            p for p in path.iterdir()
            if p.is_file() and p.suffix.lower() == ".json"
        )
        for fp in files:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if not k.startswith("_") and isinstance(v, dict):
                        merged[k] = v
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if not k.startswith("_") and isinstance(v, dict):
                    merged[k] = v
    return merged


def load_drafts(dataset_dir: Path | None, drafts_dir: Path | None) -> dict[str, str]:
    """Load draft caption texts indexed by stem."""
    drafts: dict[str, str] = {}
    if drafts_dir and drafts_dir.is_dir():
        for txt in sorted(drafts_dir.glob("*.txt")):
            drafts[txt.stem] = txt.read_text(encoding="utf-8-sig", errors="replace")
        return drafts
    if dataset_dir and dataset_dir.is_dir():
        for img in common.iter_images(dataset_dir, recursive=True):
            sib = img.with_suffix(".txt")
            if sib.exists():
                drafts[img.stem] = sib.read_text(encoding="utf-8-sig", errors="replace")
    return drafts


def _split_caption_tokens(raw_text: str) -> list[str]:
    """Split a draft/raw caption line into cleaned tokens, stripping leading comma residue."""
    text = raw_text.strip()
    if text.startswith(","):
        text = text.lstrip(",").strip()
    if "\n" in text:
        text = text.splitlines()[0]
    return [t.strip() for t in text.split(",") if t.strip()]


def _extract_source_a_tags(rec: dict[str, Any]) -> list[str]:
    """Extract raw Source A tags from a per_image.json record."""
    out: list[str] = []
    for key in ("characters_a", "tags_a", "booru_tags", "tags_full", "tags"):
        val = rec.get(key)
        if isinstance(val, list):
            out.extend(str(x).strip() for x in val if str(x).strip())
        elif isinstance(val, str) and val.strip():
            if "," in val:
                out.extend(x.strip() for x in val.split(",") if x.strip())
            else:
                out.extend(x.strip() for x in val.split() if x.strip())
        if out and key not in ("characters_a",):
            break
    return out


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


def _is_multi_person(tokens: list[str] | set[str], rules: dict[str, Any]) -> bool:
    tok_set = set(tokens)
    exemptions = rules.get("slots", {}).get("conflict_exemptions", {})
    multi_set = set(exemptions.get("multi_person_tokens", DEFAULT_MULTI_PERSON_TOKENS))
    if tok_set & multi_set:
        return True
    if ("1girl" in tok_set and "1boy" in tok_set) or ("1girl" in tok_set and "1other" in tok_set) or (
        "1boy" in tok_set and "1other" in tok_set
    ):
        return True
    return False


def _reconcile_people(slot1: list[str], rules: dict[str, Any]) -> list[str]:
    """Resolve solo vs solo focus contradiction without inventing counts."""
    tok_set = set(slot1)
    exemptions = rules.get("slots", {}).get("conflict_exemptions", {})
    multi_set = set(exemptions.get("multi_person_tokens", DEFAULT_MULTI_PERSON_TOKENS)) - {"solo focus"}
    has_other = bool(tok_set & multi_set) or (
        ("1girl" in tok_set and "1boy" in tok_set)
        or ("1girl" in tok_set and "1other" in tok_set)
        or ("1boy" in tok_set and "1other" in tok_set)
    )
    if has_other:
        return [t for t in slot1 if t != "solo"]
    return [t for t in slot1 if t != "solo focus"]


def _apply_parent_folding(
    buckets: dict[str, list[str]],
    rules: dict[str, Any],
) -> tuple[dict[str, list[str]], list[str]]:
    """Fold implied parents and generic heads when specific variants coexist."""
    all_toks = {t for toks in buckets.values() for t in toks}
    pf = rules.get("parent_fold", {})
    parent_to_children = pf.get("parent_to_children", {})
    generic_drop = set(pf.get("generic_drop_on_modifier", []))
    # Only block false-positive suffix matches (e.g. 'hair between eyes' -> 'eyes')
    false_modifier_phrases = {"hair between eyes", "covering eyes", "grabbing another's ass"}

    to_drop: set[str] = set()

    # 1. Explicit parent -> children mapping
    for parent, children in parent_to_children.items():
        if parent in all_toks and any(c in all_toks for c in children):
            to_drop.add(parent)

    # 2. Generic drop on `<modifier> <generic>`
    for generic in generic_drop:
        if generic not in all_toks:
            continue
        suffix = " " + generic
        for other in all_toks:
            if other == generic or "(" in other or other in false_modifier_phrases:
                continue
            if other.endswith(suffix):
                to_drop.add(generic)
                break

    # 3. Mouth / watermark mutual exclusions
    if MOUTH_EXCLUSIVE[0] in all_toks and MOUTH_EXCLUSIVE[1] in all_toks:
        to_drop.add(MOUTH_EXCLUSIVE[1])
    if MARK_EXCLUSIVE[0] in all_toks and MARK_EXCLUSIVE[1] in all_toks:
        to_drop.add(MARK_EXCLUSIVE[1])

    new_buckets = {
        s: [t for t in toks if t not in to_drop]
        for s, toks in buckets.items()
    }
    return new_buckets, sorted(to_drop)


def _sort_slot1(tokens: list[str], char_tags: set[str]) -> list[str]:
    counts = [t for t in tokens if PEOPLE_COUNT_RE.match(t)]
    chars = [t for t in tokens if t not in counts and (t in char_tags or r"\(" in t)]
    solo = [t for t in tokens if t in ("solo", "solo focus")]
    rels = [t for t in tokens if t in RELATION_TOKENS and t not in solo]
    used = set(counts + chars + solo + rels)
    rest = [t for t in tokens if t not in used]
    return counts + chars + solo + rels + rest


def _sort_slot2(tokens: list[str]) -> list[str]:
    def key_fn(t: str) -> tuple[int, int]:
        if any(w in t for w in ("hair", "braid", "twintail", "ponytail", "ahoge", "sidelocks", "bangs", "bun")):
            return (0, 0)
        if any(w in t for w in ("eye", "eyebrow", "eyelash", "pupil", "looking", "gaze", "heterochromia")):
            return (1, 0)
        if any(w in t for w in (
            "smile", "blush", "mouth", "teeth", "tongue", "tears", "crying",
            "frown", "pout", "smug", "grin", "expressionless", "sweatdrop",
        )):
            return (2, 0)
        if any(w in t for w in (
            "breast", "chest", "nipple", "navel", "collarbone", "stomach",
            "thigh", "hip", "waist", "skin", "ears", "horn", "halo",
            "wings", "tail", "muscle", "skinny", "tan", "nude", "breasts out",
        )):
            return (3, 0)
        if any(w in t for w in (
            "dress", "shirt", "skirt", "jacket", "coat", "sweater", "hoodie",
            "uniform", "serafuku", "bikini", "swimsuit", "bra", "panties",
            "underwear", "thighhigh", "pantyhose", "socks", "boots", "shoes",
            "gloves", "hat", "ribbon", "bow", "choker", "necklace", "earring",
            "hairband", "hair ornament", "hairclip", "glasses", "belt", "necktie",
        )):
            return (4, 0)
        return (5, 0)

    return sorted(tokens, key=key_fn)


def _sort_slot3(tokens: list[str]) -> list[str]:
    order_idx = {t: i for i, t in enumerate(DEFAULT_POSE_ORDER)}
    return sorted(tokens, key=lambda t: order_idx.get(t, len(order_idx)))


def _sort_and_dedup(
    buckets: dict[str, list[str]],
    rules: dict[str, Any],
    char_tags: set[str],
) -> dict[str, list[str]]:
    slot_order = rules.get("slots", {}).get("slot_order", SLOT_ORDER_DEFAULT)
    pose_tokens = set(rules.get("slots", {}).get("pose_tokens", []))

    # Rescue any pose tokens mistakenly placed in slot 6
    if "6" in buckets:
        moved = [t for t in buckets["6"] if t in pose_tokens]
        if moved:
            buckets["6"] = [t for t in buckets["6"] if t not in pose_tokens]
            buckets.setdefault("3", []).extend(moved)

    seen: set[str] = set()
    sorted_buckets: dict[str, list[str]] = {}
    for s in slot_order:
        toks = buckets.get(s, [])
        if s == "1":
            toks = _sort_slot1(toks, char_tags)
        elif s == "2":
            toks = _sort_slot2(toks)
        elif s == "3":
            toks = _sort_slot3(toks)
        deduped: list[str] = []
        for t in toks:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        if deduped:
            sorted_buckets[s] = deduped
    return sorted_buckets


def _has_watermark_evidence(
    sup_rec: dict[str, Any] | None,
    per_rec: dict[str, Any] | None,
    rules: dict[str, Any],
) -> bool | None:
    """Return True if evidence exists, False if explicitly absent, None if unknown."""
    if isinstance(per_rec, dict) and "watermark_present" in per_rec:
        return bool(per_rec["watermark_present"])
    if sup_rec is None:
        return None
    if "watermark_present" in sup_rec:
        return bool(sup_rec["watermark_present"])
    wm_obj = sup_rec.get("watermark")
    if isinstance(wm_obj, dict) and "present" in wm_obj:
        return bool(wm_obj["present"])
    wm_tokens = set(rules.get("slots", {}).get("watermark_tokens", []))
    slot7 = sup_rec.get("slots", {}).get("7", [])
    if slot7:
        return True
    # Supplement exists and slot 7 is empty -> no watermark/text evidence
    all_sup_toks = [t for toks in sup_rec.get("slots", {}).values() for t in toks]
    if any(t in wm_tokens for t in all_sup_toks):
        return True
    return False


def _tiered_trim(
    body_tokens: list[str],
    max_tags: int,
    rules: dict[str, Any],
    protected_extra: set[str],
) -> tuple[list[str], list[str]]:
    """Trim tokens exceeding max_tags strictly by rules/slots.json trim_tiers (tier 1 -> 4)."""
    if len(body_tokens) <= max_tags:
        return body_tokens, []

    slots_cfg = rules.get("slots", {})
    protected = (
        set(slots_cfg.get("protected_tokens", []))
        | protected_extra
        | {t for t in body_tokens if PEOPLE_COUNT_RE.match(t) or r"\(" in t}
    )

    tiers_raw = slots_cfg.get("trim_tiers", [])
    tier_lists: list[list[str]] = []
    if isinstance(tiers_raw, dict):
        for k in sorted(tiers_raw.keys()):
            tier_lists.append([str(x).lower() for x in tiers_raw[k]])
    elif isinstance(tiers_raw, list):
        for entry in tiers_raw:
            if isinstance(entry, dict):
                toks = entry.get("tokens") or entry.get("tags") or []
                tier_lists.append([str(x).lower() for x in toks])
            elif isinstance(entry, list):
                tier_lists.append([str(x).lower() for x in entry])

    current = list(body_tokens)
    trimmed: list[str] = []

    for tier_tokens in tier_lists:
        if len(current) <= max_tags:
            break
        tier_set = set(tier_tokens)
        kept: list[str] = []
        tier_trimmed: list[str] = []
        # Iterate from right to left so earlier slots/higher priority inside same tier survive first
        for tok in reversed(current):
            if len(current) - len(tier_trimmed) > max_tags and tok in tier_set and tok not in protected:
                tier_trimmed.append(tok)
            else:
                kept.append(tok)
        trimmed.extend(tier_trimmed)
        current = list(reversed(kept))

    trimmed.reverse()
    return current, trimmed


def _process_raw_token(
    raw: str,
    rules: dict[str, Any],
    tag_table: dict[str, dict[str, str]],
    char_roster: dict[str, str],
    char_tags: set[str],
    strip_prefixes: list[str],
    trigger_low: str,
    target_type: str,
) -> tuple[list[tuple[str, str | None]], set[str]]:
    """Process a single raw token through tag_table + char_roster + normalize_token."""
    low = raw.strip().lower()
    if not low or low == trigger_low:
        return [], set()

    found_chars: set[str] = set()
    # Check tag_table first
    tt_entry = tag_table.get(low) or tag_table.get(common.norm_key(low))
    if tt_entry is not None:
        action = (tt_entry.get("action") or "KEEP").strip().upper()
        slot_override = (tt_entry.get("slot") or "").strip() or None
        if action == "DELETE":
            return [], found_chars
        if action == "CHARACTER":
            mapped = char_roster.get(low) or char_roster.get(common.norm_key(low)) or raw.strip()
            if target_type == "character" and mapped.lower() == trigger_low.lstrip("@"):
                return [], found_chars
            found_chars.add(mapped.lower())
            return [(mapped.lower(), "1")], found_chars
        if action == "NORMALIZE":
            target = (tt_entry.get("target") or "").strip()
            if not target:
                return [], found_chars
            if ";" in target:
                parts = [common.norm_key(p) for p in target.split(";") if p.strip()]
                return [
                    (p, None) for p in parts
                    if p and not _is_banned(p, rules) and not common.is_negative_token(p, rules)
                ], found_chars
            normed_tok = common.norm_key(target)
            if not normed_tok or _is_banned(normed_tok, rules) or common.is_negative_token(normed_tok, rules):
                return [], found_chars
            return [(normed_tok, slot_override)], found_chars
        # KEEP
        normed_tok = common.norm_key(low)
        if normed_tok and not _is_banned(normed_tok, rules) and not common.is_negative_token(normed_tok, rules):
            return [(normed_tok, slot_override)], found_chars
        return [], found_chars

    # Check char_roster directly
    if low in char_roster or common.norm_key(low) in char_roster:
        mapped = char_roster.get(low) or char_roster[common.norm_key(low)]
        if target_type == "character" and mapped.lower() == trigger_low.lstrip("@"):
            return [], found_chars
        found_chars.add(mapped.lower())
        return [(mapped.lower(), "1")], found_chars

    # Check if already an escaped-paren character tag
    if r"\(" in raw:
        found_chars.add(low)
        return [(low, "1")], found_chars

    # Standard normalization pipeline
    normed = common.normalize_token(raw, rules, strip_char_prefixes=strip_prefixes)
    results: list[tuple[str, str | None]] = []
    for t in normed:
        t_low = t.strip().lower()
        if not t_low or t_low == trigger_low:
            continue
        if _is_banned(t_low, rules) or common.is_negative_token(t_low, rules):
            continue
        results.append((t_low, None))
    return results, found_chars


def merge_single_image(
    stem: str,
    *,
    mode: str,
    trigger: str,
    target_type: str,
    rules: dict[str, Any],
    tag_table: dict[str, dict[str, str]],
    tag_table_slots: dict[str, str],
    char_roster: dict[str, str],
    char_tags: set[str],
    per_rec: dict[str, Any] | None,
    sup_rec: dict[str, Any] | None,
    draft_text: str | None,
    override: dict[str, Any] | None,
    max_tags: int,
    trim_mode: str,
) -> tuple[str, dict[str, Any]]:
    """Merge sources for a single image stem -> (caption_line, stats_dict)."""
    trigger_clean = trigger.strip()
    trigger_low = trigger_clean.lower()

    # Check override lock early
    if override and override.get("lock") and isinstance(override.get("caption"), str) and override["caption"].strip():
        locked_cap = override["caption"].strip().splitlines()[0].strip()
        toks = [t.strip() for t in locked_cap.split(",") if t.strip()]
        body_count = max(0, len(toks) - (1 if toks and toks[0].lower() == trigger_low else 0))
        return locked_cap, {
            "tag_count": body_count,
            "trimmed_tokens": [],
            "folded_tokens": [],
            "locked": True,
            "over_max": body_count > max_tags,
        }

    strip_prefixes = sorted(
        {k.split(" (", 1)[0].split("_(", 1)[0].replace("_", " ").strip() for k in char_roster}
        | {v.split(r"\(", 1)[0].strip() for v in char_tags},
        key=len,
        reverse=True,
    )
    strip_prefixes = [p for p in strip_prefixes if p]

    buckets: dict[str, list[str]] = {s: [] for s in SLOT_ORDER_DEFAULT}
    img_char_tags: set[str] = set(char_tags)

    def add_tok(tok: str, slot: str | None) -> None:
        s = slot if slot in buckets else common.infer_slot(tok, rules, tag_table_slots=tag_table_slots, default_slot="2")
        if s not in buckets:
            s = "2"
        if tok not in buckets[s]:
            buckets[s].append(tok)

    # ---- Mode-specific merging ----
    if mode == "three-source":
        # A baseline
        if per_rec:
            for raw in _extract_source_a_tags(per_rec):
                items, found_c = _process_raw_token(
                    raw, rules, tag_table, char_roster, img_char_tags,
                    strip_prefixes, trigger_low, target_type,
                )
                img_char_tags |= found_c
                for tok, sl in items:
                    add_tok(tok, sl)
        # B fills gaps (slot 1 people from B takes priority if present)
        if sup_rec:
            b_slots = sup_rec.get("slots", {})
            b_slot1 = b_slots.get("1", [])
            if b_slot1:
                b_people: list[str] = []
                for raw in b_slot1:
                    items, found_c = _process_raw_token(
                        raw, rules, tag_table, char_roster, img_char_tags,
                        strip_prefixes, trigger_low, target_type,
                    )
                    img_char_tags |= found_c
                    for tok, _ in items:
                        if tok not in b_people:
                            b_people.append(tok)
                existing_chars = [t for t in buckets["1"] if t in img_char_tags or r"\(" in t]
                buckets["1"] = b_people + [c for c in existing_chars if c not in b_people]
            for s in ("2", "3", "4", "5", "6", "7"):
                for raw in b_slots.get(s, []):
                    items, found_c = _process_raw_token(
                        raw, rules, tag_table, char_roster, img_char_tags,
                        strip_prefixes, trigger_low, target_type,
                    )
                    img_char_tags |= found_c
                    for tok, sl in items:
                        add_tok(tok, s if sl is None else sl)

    elif mode == "two-source":
        # B is skeleton
        b_all: set[str] = set()
        if sup_rec:
            b_slots = sup_rec.get("slots", {})
            for s in SLOT_ORDER_DEFAULT:
                for raw in b_slots.get(s, []):
                    items, found_c = _process_raw_token(
                        raw, rules, tag_table, char_roster, img_char_tags,
                        strip_prefixes, trigger_low, target_type,
                    )
                    img_char_tags |= found_c
                    for tok, sl in items:
                        add_tok(tok, s if sl is None else sl)
                        b_all.add(tok)
        # C cross-checks / fills non-conflicting missing tokens
        if draft_text:
            conflict_groups = rules.get("slots", {}).get("conflict_groups", {})
            b_group_hits = {
                grp for grp, grp_toks in conflict_groups.items()
                if b_all & set(grp_toks)
            }
            for raw in _split_caption_tokens(draft_text):
                items, found_c = _process_raw_token(
                    raw, rules, tag_table, char_roster, img_char_tags,
                    strip_prefixes, trigger_low, target_type,
                )
                img_char_tags |= found_c
                for tok, sl in items:
                    if tok in b_all:
                        continue
                    inferred = sl or common.infer_slot(tok, rules, tag_table_slots=tag_table_slots, default_slot="2")
                    # Never let C overwrite/contradict B's slot 1 people counts
                    if inferred == "1" and buckets["1"] and (PEOPLE_COUNT_RE.match(tok) or tok in ("solo", "solo focus")):
                        continue
                    # Never let C add a conflicting hair_color/eye_color/hair_length if B already set one
                    if any(tok in set(conflict_groups.get(g, [])) for g in b_group_hits):
                        continue
                    add_tok(tok, inferred)

    elif mode == "vision-only":
        if sup_rec:
            b_slots = sup_rec.get("slots", {})
            for s in SLOT_ORDER_DEFAULT:
                for raw in b_slots.get(s, []):
                    items, found_c = _process_raw_token(
                        raw, rules, tag_table, char_roster, img_char_tags,
                        strip_prefixes, trigger_low, target_type,
                    )
                    img_char_tags |= found_c
                    for tok, sl in items:
                        add_tok(tok, s if sl is None else sl)

    # Reconcile people (solo vs solo focus)
    if buckets["1"]:
        buckets["1"] = _reconcile_people(buckets["1"], rules)

    # Watermark ledger consistency: omit slot 7 if image has no watermark/text evidence
    wm_evidence = _has_watermark_evidence(sup_rec, per_rec, rules)
    if wm_evidence is False:
        buckets["7"] = []

    # Parent folding & generic head drop
    buckets, folded = _apply_parent_folding(buckets, rules)

    # Multi-hair-color detection on single-character images
    all_body = [t for s in SLOT_ORDER_DEFAULT for t in buckets.get(s, [])]
    hair_colors = set(rules.get("slots", {}).get("conflict_groups", {}).get("hair_color", DEFAULT_HAIR_COLORS))
    found_hair_colors = [t for t in all_body if t in hair_colors]
    if (
        not _is_multi_person(all_body, rules)
        and len(set(found_hair_colors)) >= 2
        and "multicolored hair" not in all_body
    ):
        buckets["2"].append("multicolored hair")

    # Sort slots 1 -> 7 & deduplicate
    buckets = _sort_and_dedup(buckets, rules, img_char_tags)

    # Apply declarative overrides (remove then add, idempotent)
    override_added: set[str] = set()
    if override:
        rem_set = {common.norm_key(str(x)) for x in override.get("remove", []) if str(x).strip()}
        if rem_set:
            buckets = {
                s: [t for t in toks if t not in rem_set]
                for s, toks in buckets.items()
            }
        for raw_add in override.get("add", []):
            tok = common.norm_key(str(raw_add))
            if not tok or tok == trigger_low:
                continue
            override_added.add(tok)
            sl = common.infer_slot(tok, rules, tag_table_slots=tag_table_slots, default_slot="2")
            buckets.setdefault(sl, [])
            if tok not in buckets[sl]:
                buckets[sl].append(tok)
        buckets = _sort_and_dedup(buckets, rules, img_char_tags)

    body_tokens = [t for s in SLOT_ORDER_DEFAULT for t in buckets.get(s, [])]

    trimmed: list[str] = []
    if trim_mode == "auto" and len(body_tokens) > max_tags:
        protected_extra = img_char_tags | override_added
        body_tokens, trimmed = _tiered_trim(body_tokens, max_tags, rules, protected_extra)

    final_tokens = [trigger_clean] + body_tokens
    caption = ", ".join(final_tokens)

    return caption, {
        "tag_count": len(body_tokens),
        "trimmed_tokens": trimmed,
        "folded_tokens": folded,
        "locked": False,
        "over_max": len(body_tokens) > max_tags,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Stage 3: Merge 3-source / 2-source / vision-only captions with normalization & overrides."
    )
    ap.add_argument("--mode", choices=["three-source", "two-source", "vision-only"], required=True)
    ap.add_argument("--trigger", required=True, help="Leading trigger token (e.g. @myartist)")
    ap.add_argument("--dataset", type=Path, default=None, help="Dataset root directory")
    ap.add_argument("--out-dir", type=Path, required=True, help="Output directory for merged .txt files")
    ap.add_argument("--tag-table", type=Path, default=None, help="Optional tag_table.csv")
    ap.add_argument("--supplement", type=Path, default=None, help="Supplement JSON or shard directory")
    ap.add_argument("--per-image", type=Path, default=None, help="Optional per_image.json (Source A)")
    ap.add_argument("--drafts-dir", type=Path, default=None, help="Optional drafts .txt directory (Source C)")
    ap.add_argument("--overrides", type=Path, default=None, help="Optional declarative overrides.json")
    ap.add_argument("--char-roster", type=Path, default=None, help="Optional char_roster.json")
    ap.add_argument("--rules-dir", type=Path, default=None, help="Optional custom rules directory")
    ap.add_argument("--extra-rules", type=Path, default=None, help="Optional extra rules JSON file")
    ap.add_argument("--max-tags", type=int, default=45, help="Max tags excluding @trigger (default: 45)")
    ap.add_argument("--trim-mode", choices=["warn", "auto"], default="warn", help="warn or auto tiered trim")
    ap.add_argument(
        "--target-type",
        choices=["style", "character", "clothing", "object", "scene"],
        default="style",
        help="Target LoRA type",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    rules = common.load_rules(rules_dir=args.rules_dir, extra_rules_path=args.extra_rules)
    tag_table, tag_table_slots = load_tag_table(args.tag_table)
    char_roster, char_tags = load_char_roster(args.char_roster)
    supplements = load_supplements(args.supplement)
    per_image = common.load_per_image(args.per_image) if args.per_image and args.per_image.exists() else {}
    drafts = load_drafts(args.dataset, args.drafts_dir)
    overrides = _load_json_if_exists(args.overrides) or {}

    if args.dataset and args.dataset.is_dir():
        stems = sorted({p.stem for p in common.iter_images(args.dataset, recursive=True)})
    else:
        stems = sorted(
            set(supplements.keys()) | set(per_image.keys()) | set(drafts.keys()) | set(overrides.keys())
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report_images: dict[str, dict[str, Any]] = {}
    counts: list[int] = []

    for stem in stems:
        caption, stats = merge_single_image(
            stem,
            mode=args.mode,
            trigger=args.trigger,
            target_type=args.target_type,
            rules=rules,
            tag_table=tag_table,
            tag_table_slots=tag_table_slots,
            char_roster=char_roster,
            char_tags=char_tags,
            per_rec=per_image.get(stem),
            sup_rec=supplements.get(stem),
            draft_text=drafts.get(stem),
            override=overrides.get(stem) if isinstance(overrides, dict) else None,
            max_tags=args.max_tags,
            trim_mode=args.trim_mode,
        )
        common.atomic_write_text(args.out_dir / f"{stem}.txt", caption)
        report_images[stem] = stats
        counts.append(stats["tag_count"])

    over_max = [s for s, st in report_images.items() if st["over_max"]]
    report = {
        "mode": args.mode,
        "trigger": args.trigger,
        "target_type": args.target_type,
        "total_images": len(stems),
        "max_tags": args.max_tags,
        "trim_mode": args.trim_mode,
        "over_max_count": len(over_max),
        "tag_stats": {
            "mean": round(statistics.mean(counts), 2) if counts else 0.0,
            "median": statistics.median(counts) if counts else 0,
            "min": min(counts) if counts else 0,
            "max": max(counts) if counts else 0,
        },
        "images": report_images,
    }
    common.atomic_write_json(args.out_dir / "merge_report.json", report)
    print(
        f"Merged {len(stems)} captions -> {args.out_dir} "
        f"(mean={report['tag_stats']['mean']}, min={report['tag_stats']['min']}, "
        f"max={report['tag_stats']['max']}, over_max={len(over_max)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
