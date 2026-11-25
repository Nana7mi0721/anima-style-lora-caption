"""Shared rules loader, normalization, slot inference, atomic I/O, and HTTP helpers."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

RULES_DIR: Path = Path(__file__).resolve().parent.parent / "rules"
IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")

_SKIP_DIR_NAMES = {"thumbs", "masks", "backups"}
_SCORE_RE = re.compile(r"^score_\d+(?:_up)?$")
_NEG_PREFIX_RE = re.compile(r"^(?:no|without|not)\b")
_CJK_RE = re.compile(r"[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]")
_KAOMOJI_RE = re.compile(
    r"^(?:[:;=xX8][-^oO']?[)D3pP(/\\|]|[\^><oO0TxX;][_.oO\-wWqQ]?[\^><oO0TxX;]|uwu|owo)$"
)
_DEGREE_KEEP = {
    "very long hair",
    "very short hair",
    "very small breasts",
    "very large breasts",
    "very pale skin",
    "very dark skin",
    "very sweaty",
    "very long sleeves",
    "very long hair tied up",
    "very wide shot",
}
_PROSE_PREFIX_RE = re.compile(
    r"^(?:really|somewhat|mostly|a bit|quite|rather|uncertain|approximat|"
    r"reads as|seems|appears|possibly|probably|maybe|partially|draft|initial|"
    r"the draft|note)\b"
)
_LIGHTING_TOKENS = {
    "day",
    "night",
    "sunset",
    "sunrise",
    "evening",
    "dawn",
    "dusk",
    "twilight",
    "sunlight",
    "moonlight",
    "starlight",
    "starry sky",
    "dark",
    "shadow",
    "shadows",
    "backlight",
    "backlighting",
    "rim lighting",
    "sunbeam",
    "lens flare",
    "chiaroscuro",
    "high contrast",
    "low contrast",
    "daylight",
    "neon lights",
    "candlelight",
    "spotlight",
    "moody lighting",
    "pastel tone",
}
_USER_AGENT = "anima-style-lora-caption/1.0"


def norm_key(tag: str) -> str:
    """Normalize a tag to lowercase single-spaced form, keeping score_N(_up) underscores."""
    s = re.sub(r"\s+", " ", str(tag).strip().lower())
    if _SCORE_RE.fullmatch(s):
        return s
    return re.sub(r"\s+", " ", s.replace("_", " ")).strip()


def to_danbooru_key(tag: str) -> str:
    """Convert a tag to Danbooru underscore query key with unescaped parentheses."""
    s = str(tag).strip().lower()
    s = s.replace(r"\(", "(").replace(r"\)", ")")
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace(" ", "_")


def _read_text_lines(path: Path) -> set[str]:
    out: set[str] = set()
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(norm_key(line))
    return out


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_rules(
    rules_dir: Path | None = None,
    extra_rules_path: Path | None = None,
) -> dict[str, Any]:
    """Load SSOT rules from rules_dir and optionally merge dataset wash_rules.json."""
    rdir = Path(rules_dir) if rules_dir is not None else RULES_DIR

    vision_fix_raw = _read_json(rdir / "vision_fix.json", {})
    aliases: dict[str, str] = {}
    for k, v in vision_fix_raw.get("aliases", {}).items():
        aliases[norm_key(k)] = norm_key(v)
    multi_split: dict[str, list[str]] = {}
    for k, vals in vision_fix_raw.get("multi_split", {}).items():
        cleaned = [norm_key(x) for x in vals if str(x).strip()]
        multi_split[norm_key(k)] = cleaned
        multi_split[to_danbooru_key(k)] = cleaned
    deprecated_drop = {norm_key(x) for x in vision_fix_raw.get("deprecated_drop", []) if str(x).strip()}
    color_downgrade = {
        norm_key(k): norm_key(v)
        for k, v in vision_fix_raw.get("color_downgrade", {}).items()
        if str(k).strip() and str(v).strip()
    }
    vision_fix = {
        "aliases": aliases,
        "multi_split": multi_split,
        "deprecated_drop": sorted(deprecated_drop),
        "semantic_traps": vision_fix_raw.get("semantic_traps", {}),
        "color_downgrade": color_downgrade,
    }

    whitelist = _read_text_lines(rdir / "whitelist.txt")
    neg_keep = _read_text_lines(rdir / "neg_keep.txt")

    banned_raw = _read_json(rdir / "banned.json", {})
    banned_all: set[str] = set()
    for section in ("meta", "quality", "text_family"):
        for item in banned_raw.get(section, []):
            if str(item).strip():
                banned_all.add(norm_key(item))
    score_year_re: list[re.Pattern[str]] = [
        re.compile(pat, re.IGNORECASE)
        for pat in banned_raw.get("score_year_regex", [])
        if str(pat).strip()
    ]
    banned = {
        "meta": list(banned_raw.get("meta", [])),
        "quality": list(banned_raw.get("quality", [])),
        "text_family": list(banned_raw.get("text_family", [])),
        "score_year_regex": list(banned_raw.get("score_year_regex", [])),
        "banned_all": banned_all,
        "score_year_re": score_year_re,
    }

    parent_fold_raw = _read_json(rdir / "parent_fold.json", {})
    parent_to_children = {
        norm_key(p): [norm_key(c) for c in kids if str(c).strip()]
        for p, kids in parent_fold_raw.get("parent_to_children", {}).items()
    }
    generic_drop = [
        norm_key(x)
        for x in parent_fold_raw.get("generic_drop_on_modifier", [])
        if str(x).strip()
    ]
    parent_fold = {
        "parent_to_children": parent_to_children,
        "generic_drop_on_modifier": generic_drop,
    }

    slots = _read_json(rdir / "slots.json", {})
    prompt_templates = _read_json(rdir / "prompt_templates.json", {})

    if extra_rules_path is not None:
        epath = Path(extra_rules_path)
        if epath.is_file():
            extra = json.loads(epath.read_text(encoding="utf-8"))
            for k, v in extra.get("aliases", {}).items():
                aliases[norm_key(k)] = norm_key(v)
            for item in extra.get("whitelist", []):
                if str(item).strip():
                    whitelist.add(norm_key(item))
            for item in extra.get("neg_keep", []):
                if str(item).strip():
                    neg_keep.add(norm_key(item))
            for item in extra.get("drop_exact", []):
                if str(item).strip():
                    banned_all.add(norm_key(item))
            for pat in extra.get("drop_regex", []):
                if str(pat).strip():
                    score_year_re.append(re.compile(pat, re.IGNORECASE))
            for item in extra.get("deprecated_drop", []):
                if str(item).strip():
                    deprecated_drop.add(norm_key(item))
            for k, v in extra.get("color_downgrade", {}).items():
                if str(k).strip() and str(v).strip():
                    color_downgrade[norm_key(k)] = norm_key(v)
            for k, vals in extra.get("multi_split", {}).items():
                cleaned = [norm_key(x) for x in vals if str(x).strip()]
                multi_split[norm_key(k)] = cleaned
                multi_split[to_danbooru_key(k)] = cleaned
            vision_fix["deprecated_drop"] = sorted(deprecated_drop)

    return {
        "vision_fix": vision_fix,
        "whitelist": whitelist,
        "neg_keep": neg_keep,
        "banned": banned,
        "parent_fold": parent_fold,
        "slots": slots,
        "prompt_templates": prompt_templates,
    }


def is_negative_token(
    tag: str,
    rules: dict[str, Any],
    valid_dict: set[str] | None = None,
) -> bool:
    """Return True if tag is a disallowed negative-prefix token."""
    key = norm_key(tag)
    if not key or not _NEG_PREFIX_RE.match(key):
        return False
    if key in rules.get("neg_keep", set()):
        return False
    if valid_dict is not None and to_danbooru_key(key) in valid_dict:
        return False
    return True


def _is_banned(tag: str, rules: dict[str, Any]) -> bool:
    banned = rules.get("banned", {})
    if tag in banned.get("banned_all", set()):
        return True
    db_key = to_danbooru_key(tag)
    for rx in banned.get("score_year_re", []):
        if rx.match(tag) or rx.match(db_key):
            return True
    return False


def _clean_parens_or_reject(s: str) -> str | None:
    """Handle parenthetical wrappers/comments; return None on unmatched split fragments."""
    # Preserve escaped Danbooru character qualifiers like fern \(sousou no frieren\).
    if r"\(" in s or r"\)" in s:
        unescaped = s.replace(r"\(", "").replace(r"\)", "")
        if "(" in unescaped or ")" in unescaped:
            return None
        return s

    if s.count("(") != s.count(")") or s.count("[") != s.count("]") or s.count("{") != s.count("}"):
        return None

    # Strip balanced outer wrappers like ((sky)) or (white shirt)
    while len(s) >= 2 and s[0] in "([{" and ((s[0] == "(" and s[-1] == ")") or (s[0] == "[" and s[-1] == "]") or (s[0] == "{" and s[-1] == "}")):
        inner = s[1:-1].strip()
        if inner.count("(") == inner.count(")"):
            s = inner
        else:
            break

    # Strip inner parenthetical comments containing spaces (e.g. "halo (above head)")
    # while keeping compact Danbooru disambiguations like "bat (animal)" or "star (sky)".
    s = re.sub(r"\s*\([^()]*\s+[^()]*\)", "", s).strip()
    return s


def normalize_token(
    raw: str,
    rules: dict[str, Any],
    valid_dict: set[str] | None = None,
    strip_char_prefixes: set[str] | None = None,
) -> list[str]:
    """Normalize a raw tag string into zero, one, or multiple canonical tokens."""
    if raw is None:
        return []
    s = str(raw).strip()
    if not s:
        return []

    # Preserve kaomoji before punctuation stripping
    s_lower_raw = s.strip().lower()
    if _KAOMOJI_RE.fullmatch(s_lower_raw):
        return [s_lower_raw]

    s = s.strip("\"'`*•·,")
    s = s.rstrip(".!?;:")
    s = s.strip()
    if not s:
        return []

    if _KAOMOJI_RE.fullmatch(s.lower()):
        return [s.lower()]

    if _CJK_RE.search(s):
        return []

    cleaned = _clean_parens_or_reject(s)
    if cleaned is None:
        return []

    t = norm_key(cleaned)
    if not t or t in {"none", "n/a", "na", "-", "null"}:
        return []

    if re.fullmatch(r"[\d\s\-_/.,:;!?()]+", t):
        return []

    # Reject long prose sentences
    if len(t.split()) > 6 or len(t) > 80:
        return []

    # Whitelist check BEFORE character-prefix stripping (e.g. "bright indoor light")
    whitelist: set[str] = rules.get("whitelist", set())
    if t in whitelist:
        return [t]

    # Strip known character-name prefixes (longest first)
    if strip_char_prefixes:
        for prefix in sorted((norm_key(p) for p in strip_char_prefixes if str(p).strip()), key=len, reverse=True):
            if t.startswith(prefix + " "):
                t = t[len(prefix) + 1:].strip()
                break
        if not t:
            return []
        if t in whitelist:
            return [t]

    # Colloquial / oral rewrites
    if t.startswith("slightly "):
        t = t[len("slightly "):].strip()
        if not t:
            return []
    if re.match(r"^close[- ]up on\b", t):
        t = "close-up"

    if t not in _DEGREE_KEEP and _PROSE_PREFIX_RE.match(t):
        return []
    if re.match(r"^(?:partially|barely|hardly|mostly|clearly)\b.*\bvisible$", t):
        return []

    t = norm_key(t)
    vfix = rules.get("vision_fix", {})
    dep_drop = {norm_key(x) for x in vfix.get("deprecated_drop", [])}
    aliases = vfix.get("aliases", {})
    multi_split = vfix.get("multi_split", {})
    color_downgrade = vfix.get("color_downgrade", {})

    db_t = to_danbooru_key(t)
    if t in dep_drop or db_t in dep_drop:
        return []
    if _is_banned(t, rules):
        return []

    if t in multi_split or db_t in multi_split:
        parts = multi_split.get(t) or multi_split.get(db_t) or []
        out: list[str] = []
        for part in parts:
            pk = norm_key(part)
            if pk and pk not in dep_drop and not _is_banned(pk, rules) and not is_negative_token(pk, rules, valid_dict):
                out.append(pk)
        return out

    if t in aliases:
        t = norm_key(aliases[t])
    elif db_t in aliases:
        t = norm_key(aliases[db_t])

    if t in color_downgrade:
        t = norm_key(color_downgrade[t])

    if is_negative_token(t, rules, valid_dict):
        return []

    if t in dep_drop or to_danbooru_key(t) in dep_drop or _is_banned(t, rules):
        return []

    if len(t) < 2 and t not in {"v", "w"}:
        return []

    return [t]


def infer_slot(
    tag: str,
    rules: dict[str, Any],
    tag_table_slots: dict[str, str] | None = None,
    default_slot: str = "2",
) -> str:
    """Determine slot ('1'..'7') for a normalized tag."""
    t = norm_key(tag)
    slots_cfg = rules.get("slots", {})
    valid_slots = set(slots_cfg.get("slot_order", ["1", "2", "3", "4", "5", "6", "7"]))

    if tag_table_slots:
        explicit = tag_table_slots.get(t) or tag_table_slots.get(str(tag).strip().lower())
        if explicit and str(explicit) in valid_slots:
            return str(explicit)

    slot1_people = set(slots_cfg.get("slot1_people", []))
    if t in slot1_people or re.match(r"^\d+\+?(?:girl|boy|other)s?$", t) or t.startswith("multiple "):
        return "1"

    watermark_tokens = set(slots_cfg.get("watermark_tokens", []))
    if t in watermark_tokens:
        return "7"

    composition_tokens = set(slots_cfg.get("composition_tokens", []))
    if t in composition_tokens:
        return "6"

    pose_tokens = set(slots_cfg.get("pose_tokens", []))
    if t in pose_tokens:
        return "3"

    if t in _LIGHTING_TOKENS:
        return "5"

    if t in rules.get("whitelist", set()):
        return "5"

    if re.search(
        r"\b(?:lighting|tone|shadows?|contrast|sunlight|moonlight|starlight|daylight)\b",
        t,
    ) and not any(w in t for w in ("hair", "eyes", "skin", "dress", "shirt", "skirt", "pants", "shoes", "boots")):
        return "5"

    return default_slot


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 text (no BOM, exact newline bytes) to path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: object) -> None:
    """Atomically write UTF-8 JSON with ensure_ascii=False."""
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, payload)


def load_per_image(path: Path) -> dict[str, dict[str, Any]]:
    """Load per_image.json if present, returning an empty dict if missing."""
    p = Path(path)
    if not p.is_file():
        return {}
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {p}, got {type(data).__name__}")
    return data


def upsert_per_image(path: Path, records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Incrementally merge records into per_image.json without deleting existing stems."""
    existing = load_per_image(path)
    for stem, rec in records.items():
        if stem in existing and isinstance(existing[stem], dict) and isinstance(rec, dict):
            merged = dict(existing[stem])
            merged.update(rec)
            existing[stem] = merged
        else:
            existing[stem] = rec
    atomic_write_json(path, existing)
    return existing


def iter_images(root: Path, recursive: bool = True) -> list[Path]:
    """Iterate image files under root, skipping _, ., thumbs, masks, and backups dirs."""
    root_path = Path(root)
    if not root_path.exists():
        return []

    found: list[Path] = []
    if not recursive:
        for entry in root_path.iterdir():
            if entry.is_file() and entry.suffix.lower() in IMAGE_EXTS:
                found.append(entry)
    else:
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(("_", ".")) and d.lower() not in _SKIP_DIR_NAMES
            )
            for fname in sorted(filenames):
                p = Path(dirpath) / fname
                if p.suffix.lower() in IMAGE_EXTS:
                    found.append(p)

    return sorted(found, key=lambda p: str(p).lower())


def _build_url(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    filtered = {k: v for k, v in params.items() if v is not None}
    if not filtered:
        return url
    qs = urllib.parse.urlencode(filtered, doseq=True)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{qs}"


def http_get_json(
    url: str,
    params: dict[str, Any] | None = None,
    proxy: str | None = None,
    auth: tuple[str, str] | None = None,
    timeout: int = 30,
    retries: int = 3,
    use_curl: bool = False,
) -> object:
    """Fetch JSON over HTTP via urllib or curl subprocess; raise RuntimeError on persistent failure."""
    full_url = _build_url(url, params)
    attempts = max(1, int(retries))
    last_err: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            if use_curl:
                cmd = ["curl", "-sS", "-f", "--max-time", str(timeout), "-A", _USER_AGENT]
                if proxy:
                    cmd.extend(["-x", proxy])
                if auth:
                    cmd.extend(["-u", f"{auth[0]}:{auth[1]}"])
                cmd.append(full_url)
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout + 5,
                    check=False,
                )
                if proc.returncode != 0:
                    raise RuntimeError(f"curl exited {proc.returncode}: {proc.stderr.strip()}")
                return json.loads(proc.stdout)

            headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
            if auth:
                token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
            req = urllib.request.Request(full_url, headers=headers)
            handlers: list[Any] = []
            if proxy:
                handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < attempts:
                time.sleep(min(2.0, 0.4 * attempt))

    raise RuntimeError(f"HTTP GET failed for {full_url} after {attempts} attempt(s): {last_err}")
