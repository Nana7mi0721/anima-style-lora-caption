#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 5: Snapshot backup, atomic writeback, post-write verify, and caption_summary.json handoff."""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
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

META_EXTS = {".json", ".csv"}


def _check_validation_gate(report_path: Path | None, force_unverified: bool) -> list[str]:
    """Return list of blocking reasons from validation_report JSON."""
    if report_path is None:
        return []
    if not report_path.exists():
        return [f"Validation report not found: {report_path}"]
    data = json.loads(report_path.read_text(encoding="utf-8"))
    summary = data.get("summary", {})
    blocks: list[str] = []
    if summary.get("total_offline_errors", 0) > 0:
        blocks.append(f"Validation report has {summary['total_offline_errors']} offline error(s)")
    if summary.get("online_invalid", 0) > 0:
        blocks.append(f"Validation report has {summary['online_invalid']} INVALID online tag(s)")
    if summary.get("online_unverified", 0) > 0 and not force_unverified:
        blocks.append(
            f"Validation report has {summary['online_unverified']} UNVERIFIED online tag(s) "
            "(pass --force-unverified to override)"
        )
    return blocks


def _snapshot_backup(dataset_dir: Path, backup_root: Path) -> tuple[Path, int]:
    """Snapshot all existing .txt drafts (exact bytes) and metadata files into timestamped dir."""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = backup_root / f"{dataset_dir.name}_{stamp}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    backup_resolved = backup_root.resolve()

    copied = 0
    for path in sorted(dataset_dir.rglob("*")):
        if not path.is_file():
            continue
        # Avoid recursing into backup_root if located inside dataset_dir
        try:
            path.resolve().relative_to(backup_resolved)
            continue
        except ValueError:
            pass
        if path.suffix.lower() == ".txt" or path.suffix.lower() in META_EXTS:
            rel = path.relative_to(dataset_dir)
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    return dest_dir, copied


def _post_write_verify(written_paths: list[Path], trigger: str | None) -> list[str]:
    """Re-read raw bytes of every written .txt on disk and verify formatting invariants."""
    failures: list[str] = []
    trigger_low = trigger.strip().lower() if trigger else None
    for path in written_paths:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            failures.append(f"{path.name}: contains UTF-8 BOM")
            continue
        if b"\r" in raw:
            failures.append(f"{path.name}: contains CR")
            continue
        text = raw.decode("utf-8")
        if text.endswith("\n"):
            failures.append(f"{path.name}: has trailing newline")
            continue
        if "\n" in text:
            failures.append(f"{path.name}: multiline content")
            continue
        if trigger_low:
            first = text.split(",", 1)[0].strip()
            if first != trigger_low:
                failures.append(f"{path.name}: first token {first!r} != {trigger_low!r}")
    return failures


def _collect_characters(
    char_roster_path: Path | None,
    written_texts: list[str],
) -> list[str]:
    if char_roster_path and char_roster_path.exists():
        roster = json.loads(char_roster_path.read_text(encoding="utf-8"))
        if isinstance(roster, dict):
            chars = []
            for info in roster.values():
                if isinstance(info, dict) and info.get("anima_tag"):
                    chars.append(str(info["anima_tag"]))
            return sorted(set(chars))
    found: set[str] = set()
    for text in written_texts:
        for tok in text.split(","):
            t = tok.strip()
            if r"\(" in t:
                found.add(t)
    return sorted(found)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Stage 5: Snapshot backup, atomic writeback, and caption_summary.json handoff."
    )
    ap.add_argument("--staged-dir", type=Path, required=True, help="Directory of validated staged .txt captions")
    ap.add_argument("--dataset", type=Path, required=True, help="Target dataset root directory")
    ap.add_argument("--backup-dir", type=Path, default=None, help="Backup root directory (default: <dataset_parent>/_backup)")
    ap.add_argument("--trigger", type=str, default=None, help="Trigger token (e.g. @myartist)")
    ap.add_argument(
        "--target-type",
        choices=["style", "character", "clothing", "object", "scene"],
        default="style",
        help="Target LoRA type for anima-lora-config handoff",
    )
    ap.add_argument("--validation-report", type=Path, default=None, help="Validation JSON report from validate_captions.py")
    ap.add_argument("--char-roster", type=Path, default=None, help="Optional char_roster.json")
    ap.add_argument("--out-summary", type=Path, default=Path("caption_summary.json"), help="Output caption_summary.json path")
    ap.add_argument("--apply", action="store_true", help="Actually write back to dataset (dry-run by default)")
    ap.add_argument("--force-unverified", action="store_true", help="Allow writeback when online_unverified > 0")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    blocks = _check_validation_gate(args.validation_report, args.force_unverified)
    if blocks:
        for b in blocks:
            print(f"BLOCKED: {b}", file=sys.stderr)
        return 1

    if not args.dataset.is_dir():
        print(f"Error: dataset directory not found: {args.dataset}", file=sys.stderr)
        return 2
    if not args.staged_dir.is_dir():
        print(f"Error: staged directory not found: {args.staged_dir}", file=sys.stderr)
        return 2

    images = common.iter_images(args.dataset, recursive=True)
    pairs: list[tuple[Path, Path]] = []
    missing_staged: list[str] = []
    for img in images:
        staged = args.staged_dir / f"{img.stem}.txt"
        if staged.exists():
            pairs.append((img, staged))
        else:
            missing_staged.append(img.stem)

    if missing_staged:
        print(
            f"Error: {len(missing_staged)} dataset image(s) missing staged caption: {missing_staged[:10]}",
            file=sys.stderr,
        )
        return 1

    backup_root = args.backup_dir or (args.dataset.parent / "_backup")

    if not args.apply:
        print(
            f"[DRY-RUN] Would snapshot existing drafts/metadata under {args.dataset} -> {backup_root}, "
            f"then write {len(pairs)} staged captions to dataset siblings. Pass --apply to execute."
        )
        return 0

    backup_path, backed_count = _snapshot_backup(args.dataset, backup_root)
    print(f"Backed up {backed_count} file(s) -> {backup_path}")

    written_paths: list[Path] = []
    written_texts: list[str] = []
    tag_counts: list[int] = []
    trigger_low = args.trigger.strip().lower() if args.trigger else None

    for img, staged in pairs:
        content = staged.read_text(encoding="utf-8-sig").rstrip("\r\n")
        dest = img.with_suffix(".txt")
        common.atomic_write_text(dest, content)
        written_paths.append(dest)
        written_texts.append(content)
        toks = [t.strip() for t in content.split(",") if t.strip()]
        if toks and ((trigger_low and toks[0].lower() == trigger_low) or (not trigger_low and toks[0].startswith("@"))):
            tag_counts.append(len(toks) - 1)
        else:
            tag_counts.append(len(toks))

    verify_failures = _post_write_verify(written_paths, args.trigger)
    if verify_failures:
        for f in verify_failures:
            print(f"POST-WRITE VERIFY FAILED: {f}", file=sys.stderr)
        return 1

    summary = {
        "dataset": str(args.dataset.resolve()),
        "dataset_name": args.dataset.name,
        "trigger": args.trigger,
        "target_type": args.target_type,
        "keep_tokens": 1 if args.trigger else 0,
        "caption_extension": ".txt",
        "image_count": len(images),
        "caption_count": len(written_paths),
        "tag_stats": {
            "mean": round(statistics.mean(tag_counts), 2) if tag_counts else 0.0,
            "median": statistics.median(tag_counts) if tag_counts else 0,
            "min": min(tag_counts) if tag_counts else 0,
            "max": max(tag_counts) if tag_counts else 0,
        },
        "backup_path": str(backup_path.resolve()),
        "characters": _collect_characters(args.char_roster, written_texts),
        "applied_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "post_write_verified": True,
    }
    common.atomic_write_json(args.out_summary, summary)
    print(f"Applied {len(written_paths)} captions (post-write verified) -> summary: {args.out_summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
