#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comprehensive pipeline tests (compatible with both pytest and python -m unittest)."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
RULES_DIR = REPO_ROOT / "rules"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import apply_captions  # noqa: E402
import build_tag_table  # noqa: E402
import common  # noqa: E402
import lookup_chars  # noqa: E402
import make_thumbs  # noqa: E402
import merge_captions  # noqa: E402
import parse_descriptions  # noqa: E402
import validate_captions  # noqa: E402


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise AssertionError(f"Duplicate JSON key detected: {k!r}")
        out[k] = v
    return out


def _make_filler_tags(n: int) -> list[str]:
    base = [
        "1girl", "solo", "blonde hair", "long hair", "blue eyes", "smile",
        "looking at viewer", "blush", "open mouth", "small breasts",
        "white dress", "frills", "ribbon", "shoes", "standing", "arms up",
        "indoors", "window", "curtains", "day", "soft lighting", "warm tone",
        "upper body", "cowboy shot", "straight-on", "thighhighs", "collarbone",
        "navel", "bare shoulders", "sidelocks", "ahoge", "hairclip", "choker",
        "bracelet", "earrings", "table", "chair", "bed", "pillow", "sunlight",
        "bokeh", "depth of field", "blurry background", "simple background",
        "pleated skirt", "necktie", "cardigan", "loafers", "socks",
    ]
    return base[:n]


class TestRulesIntegrity(unittest.TestCase):
    def test_all_json_rules_valid_and_no_duplicate_keys(self) -> None:
        json_files = sorted(RULES_DIR.glob("*.json"))
        self.assertGreaterEqual(len(json_files), 5)
        for jf in json_files:
            raw = jf.read_text(encoding="utf-8")
            data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
            self.assertIsInstance(data, dict, f"{jf.name} should be a JSON object")

    def test_neg_keep_no_duplicates_and_no_typos(self) -> None:
        raw_lines = [
            ln.strip()
            for ln in (RULES_DIR / "neg_keep.txt").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        self.assertEqual(len(raw_lines), len(set(raw_lines)), "Duplicate entries in neg_keep.txt")
        self.assertIn("no panties", raw_lines)
        self.assertIn("no bra", raw_lines)
        self.assertIn("no humans", raw_lines)

    def test_load_rules_contract(self) -> None:
        rules = common.load_rules()
        for key in ("vision_fix", "whitelist", "neg_keep", "banned", "parent_fold", "slots", "prompt_templates"):
            self.assertIn(key, rules)
        self.assertIn("banned_all", rules["banned"])
        self.assertIn("score_year_re", rules["banned"])


class TestMakeThumbs(unittest.TestCase):
    def test_large_resize_rgba_flatten_and_skip_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dst = root / "thumbs"
            src.mkdir()

            large = Image.new("RGB", (3000, 1500), (120, 40, 200))
            large.save(src / "0001.png")

            rgba = Image.new("RGBA", (800, 600), (255, 0, 0, 128))
            rgba.save(src / "0002.png")

            stats1 = make_thumbs.run_make_thumbs(src, dst, long_edge=1536, max_mb=2.0)
            self.assertEqual(stats1["created"], 2)
            self.assertEqual(stats1["skipped"], 0)
            self.assertEqual(stats1["failed"], 0)

            with Image.open(dst / "0001.jpg") as im1:
                self.assertEqual(im1.mode, "RGB")
                self.assertLessEqual(max(im1.size), 1536)
            with Image.open(dst / "0002.jpg") as im2:
                self.assertEqual(im2.mode, "RGB")

            # Re-run skips existing
            stats2 = make_thumbs.run_make_thumbs(src, dst, long_edge=1536, max_mb=2.0)
            self.assertEqual(stats2["created"], 0)
            self.assertEqual(stats2["skipped"], 2)


class TestBuildTagTable(unittest.TestCase):
    def test_gap_matrix_char_freq_and_incremental_per_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ds = root / "dataset"
            ds.mkdir()
            for i in range(1, 5):
                Image.new("RGB", (64, 64)).save(ds / f"000{i}.jpg")
                (ds / f"000{i}.txt").write_text(
                    ", @myart, 1girl, solo, katou_megumi_(saekano), blue_eyes, soft lighting",
                    encoding="utf-8",
                )

            per_img = root / "per_image.json"
            common.atomic_write_json(per_img, {"legacy_stem": {"tags_a": ["1boy"], "custom_note": "keep"}})

            out_table = root / "tag_table.csv"
            out_todo = root / "review_todo.csv"
            out_chars = root / "char_freq.csv"

            stats = build_tag_table.run_build_tag_table(
                dataset=ds,
                out_table=out_table,
                out_todo=out_todo,
                out_chars=out_chars,
                per_image_path=per_img,
                min_char_freq=4,
                trigger="@myart",
            )
            self.assertEqual(stats["images"], 4)

            with out_todo.open("r", encoding="utf-8-sig") as fh:
                todo_rows = list(csv.DictReader(fh))
            self.assertEqual(len(todo_rows), 4)
            self.assertIn("missing_slots", todo_rows[0])
            self.assertIn("preset", todo_rows[0])

            with out_chars.open("r", encoding="utf-8-sig") as fh:
                char_rows = list(csv.DictReader(fh))
            self.assertEqual(len(char_rows), 1)
            self.assertEqual(char_rows[0]["count"], "4")
            self.assertEqual(char_rows[0]["meets_threshold"], "true")

            per_data = common.load_per_image(per_img)
            self.assertIn("legacy_stem", per_data, "Incremental upsert must not truncate pre-existing stems")
            self.assertIn("0001", per_data)


class TestParseDescriptions(unittest.TestCase):
    def test_json_and_legacy_parsing_defenses_and_crop_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desc = root / "desc"
            desc.mkdir()
            crops = root / "crops"
            (crops / "agent_a").mkdir(parents=True)
            (crops / "agent_a" / "0001_crop.jpg").write_bytes(b"jpg")

            # 1. Strict JSON with misplaced pose in COMPOSITION and bright indoor light whitelist
            (desc / "0001.json").write_text(
                json.dumps({
                    "PEOPLE": ["1girl", "solo"],
                    "HAIR": ["blonde hair"],
                    "EYES": ["blue eyes"],
                    "POSE": ["sitting"],
                    "COMPOSITION": ["standing", "cowboy shot"],
                    "LIGHTING": ["bright indoor light"],
                    "WATERMARK": ["none"],
                }),
                encoding="utf-8",
            )

            # 2. Legacy KEY: val with parenthetical comment, kaomoji :3, and no cum negative
            (desc / "0002.txt").write_text(
                "\n".join([
                    "PEOPLE: 1girl, solo",
                    "EXPRESSION: smile (slightly parted), :3",
                    "COMPOSITION: kneeling, upper body",
                    "FLUIDS: no cum",
                    "WATERMARK: none",
                ]),
                encoding="utf-8",
            )

            # 3. Refused -> failure ledger without fallback
            (desc / "0003.txt").write_text("Sorry, I cannot process this image.", encoding="utf-8")

            out_json = root / "supplement.json"
            out_failed = root / "failed.txt"
            out_review = root / "review.txt"
            stats = parse_descriptions.run_parse_descriptions(
                desc_dir=desc,
                out_json=out_json,
                out_failed=out_failed,
                out_review=out_review,
                crop_root=crops,
            )
            self.assertEqual(stats["parsed"], 2)
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(stats["cleaned_crops"], 1)
            self.assertFalse((crops / "agent_a").exists())

            sup = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertIn("standing", sup["0001"]["slots"]["3"])
            self.assertIn("sitting", sup["0001"]["slots"]["3"])
            self.assertIn("bright indoor light", sup["0001"]["slots"]["5"])
            self.assertIn(":3", sup["0002"]["slots"]["2"])
            self.assertIn("kneeling", sup["0002"]["slots"]["3"])
            self.assertNotIn("no cum", [t for v in sup["0002"]["slots"].values() for t in v])

            failed_text = out_failed.read_text(encoding="utf-8")
            self.assertIn("0003\trefused", failed_text)
            self.assertNotIn("0003", sup)

    def test_whitelist_preserved_before_char_prefix_strip(self) -> None:
        rules = common.load_rules()
        res = common.normalize_token(
            "bright indoor light",
            rules,
            strip_char_prefixes={"bright"},
        )
        self.assertEqual(res, ["bright indoor light"])


class TestLookupChars(unittest.TestCase):
    def test_animadex_resolution_and_offline_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            freq_csv = root / "char_freq.csv"
            freq_csv.write_text("tag,count\nkatou_megumi_(saekano),10\nlow_freq,2\n", encoding="utf-8")
            out_json = root / "char_roster.json"
            cache_path = root / "char_cache.jsonl"

            fake_resp = {
                "results": [
                    {
                        "slug": "katou_megumi_(saekano)",
                        "name": "katou megumi",
                        "trigger": "katou megumi, saenai heroine no sodatekata",
                        "copyright": "saenai heroine no sodatekata",
                    }
                ]
            }
            with mock.patch("common.http_get_json", return_value=fake_resp):
                rc = lookup_chars.main([
                    "--char-freq", str(freq_csv),
                    "--min-freq", "4",
                    "--out", str(out_json),
                    "--cache", str(cache_path),
                ])
            self.assertEqual(rc, 0)
            roster = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertIn("katou_megumi_(saekano)", roster)
            self.assertNotIn("low_freq", roster)
            self.assertEqual(roster["katou_megumi_(saekano)"]["anima_tag"], "katou megumi")
            self.assertTrue(roster["katou_megumi_(saekano)"]["confirmed"])

            # Offline fallback via local dict
            local_dict = root / "dict.csv"
            local_dict.write_text("name,post_count\nfrieren,10000\n", encoding="utf-8")
            out_off = root / "char_offline.json"
            rc_off = lookup_chars.main([
                "--tags", "frieren",
                "--out", str(out_off),
                "--local-dict", str(local_dict),
                "--offline",
            ])
            self.assertEqual(rc_off, 0)
            off_roster = json.loads(out_off.read_text(encoding="utf-8"))
            self.assertEqual(off_roster["frieren"]["source"], "local_dict")
            self.assertFalse(off_roster["frieren"]["confirmed"])


class TestMergeCaptions(unittest.TestCase):
    def test_three_modes_folding_overrides_and_tiered_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ds = root / "dataset"
            ds.mkdir()
            for stem in ("0001", "0002", "0003"):
                Image.new("RGB", (64, 64)).save(ds / f"{stem}.png")

            per_img = root / "per_image.json"
            common.atomic_write_json(per_img, {
                "0001": {"tags_a": ["1girl", "cat ears", "animal ears", "white dress", "dress", "watermark"]},
            })
            sup = root / "supplement.json"
            common.atomic_write_json(sup, {
                "0001": {
                    "slots": {
                        "1": ["1girl", "solo"],
                        "2": ["blue hair", "red hair", "blue eyes", "smile"],
                        "3": ["sitting"],
                        "4": ["indoors"],
                        "5": ["day", "soft lighting"],
                        "6": ["upper body"],
                    },
                    "watermark": {"present": False, "tokens": []},
                },
                "0002": {
                    "slots": {
                        "1": ["1girl", "solo"],
                        "2": ["black hair", "long hair", "green eyes", "blush", "sweat", "saliva", "jewelry", "earrings", "ribbon", "frills"],
                        "3": ["standing"],
                        "4": ["window", "curtains", "pillow", "couch", "bed", "chair", "table", "wall", "floor", "indoors", "simple background"],
                        "5": ["soft shadows", "low contrast", "warm tone", "cool tone", "backlighting", "rim lighting", "side lighting", "bokeh", "depth of field", "blurry background"],
                        "6": ["upper body", "cowboy shot"],
                    },
                    "watermark": {"present": False, "tokens": []},
                },
                "0003": {
                    "slots": {"1": ["1girl", "solo"], "2": ["pink hair"], "6": ["portrait"]},
                    "watermark": {"present": False, "tokens": []},
                },
            })
            drafts = root / "drafts"
            drafts.mkdir()
            (drafts / "0001.txt").write_text(", @myart, 1girl, red eyes, uncensored", encoding="utf-8")
            (drafts / "0002.txt").write_text(", @myart, 1girl, solo", encoding="utf-8")
            (drafts / "0003.txt").write_text(", @myart, 1girl, solo", encoding="utf-8")

            overrides = root / "overrides.json"
            common.atomic_write_json(overrides, {
                "0001": {"add": ["ahoge"], "remove": ["smile"]},
                "0002": {"add": ["soft shadows"], "remove": []},
                "0003": {"lock": True, "caption": "@myart, 1girl, solo, custom locked token"},
            })

            # 1. Three-source check (0001 folds animal ears & dress, detects multicolored hair, omits watermark)
            out3 = root / "out3"
            rc = merge_captions.main([
                "--mode", "three-source",
                "--trigger", "@myart",
                "--dataset", str(ds),
                "--out-dir", str(out3),
                "--supplement", str(sup),
                "--per-image", str(per_img),
                "--overrides", str(overrides),
                "--max-tags", "25",
                "--trim-mode", "auto",
            ])
            self.assertEqual(rc, 0)
            raw1 = (out3 / "0001.txt").read_bytes()
            self.assertFalse(raw1.endswith(b"\n"))
            cap1 = raw1.decode("utf-8")
            toks1 = [t.strip() for t in cap1.split(",")]
            self.assertEqual(toks1[0], "@myart")
            self.assertIn("cat ears", toks1)
            self.assertNotIn("animal ears", toks1)
            self.assertIn("white dress", toks1)
            self.assertNotIn("dress", toks1)
            self.assertIn("multicolored hair", toks1)
            self.assertNotIn("watermark", toks1)
            self.assertIn("ahoge", toks1)
            self.assertNotIn("smile", toks1)

            # Locked 0003 preserved verbatim
            self.assertEqual((out3 / "0003.txt").read_text(encoding="utf-8"), "@myart, 1girl, solo, custom locked token")

            # Auto-trim respects protected tokens + overrides.add (`soft shadows`) on 0002
            toks2 = [t.strip() for t in (out3 / "0002.txt").read_text(encoding="utf-8").split(",")]
            self.assertLessEqual(len(toks2) - 1, 25)
            self.assertIn("1girl", toks2)
            self.assertIn("solo", toks2)
            self.assertIn("soft shadows", toks2)

            # Idempotency: re-run produces identical output
            out3_b = root / "out3_b"
            merge_captions.main([
                "--mode", "three-source",
                "--trigger", "@myart",
                "--dataset", str(ds),
                "--out-dir", str(out3_b),
                "--supplement", str(sup),
                "--per-image", str(per_img),
                "--overrides", str(overrides),
                "--max-tags", "25",
                "--trim-mode", "auto",
            ])
            self.assertEqual((out3 / "0001.txt").read_bytes(), (out3_b / "0001.txt").read_bytes())

            # 2. Two-source: C cross-checks without overwriting B's blue eyes with red eyes
            out2 = root / "out2"
            merge_captions.main([
                "--mode", "two-source",
                "--trigger", "@myart",
                "--dataset", str(ds),
                "--out-dir", str(out2),
                "--supplement", str(sup),
                "--drafts-dir", str(drafts),
            ])
            cap2_1 = (out2 / "0001.txt").read_text(encoding="utf-8")
            self.assertIn("blue eyes", cap2_1)
            self.assertNotIn("red eyes", cap2_1)
            self.assertNotIn("uncensored", cap2_1)

            # 3. Vision-only ignores A and C
            outv = root / "outv"
            merge_captions.main([
                "--mode", "vision-only",
                "--trigger", "@myart",
                "--dataset", str(ds),
                "--out-dir", str(outv),
                "--supplement", str(sup),
                "--per-image", str(per_img),
                "--drafts-dir", str(drafts),
            ])
            capv_1 = (outv / "0001.txt").read_text(encoding="utf-8")
            self.assertNotIn("cat ears", capv_1)
            self.assertNotIn("red eyes", capv_1)

    def test_underscore_per_image_with_tag_table_normalizes_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ds = root / "ds"
            ds.mkdir()
            Image.new("RGB", (64, 64)).save(ds / "0001.png")

            per_img = root / "per_image.json"
            common.atomic_write_json(per_img, {
                "0001": {
                    "tags_a": [
                        "no_humans", "white_shirt", "pleated_skirt",
                        "outdoors", "blue_sky", "cloud", "tree", "grass",
                        "day", "sunlight", "wide_shot", "scenery",
                        "building", "window", "bench", "path", "flower",
                        "sunbeam", "shadows", "warm_tone",
                    ],
                },
            })
            tag_table = root / "tag_table.csv"
            build_tag_table.run_build_tag_table(
                dataset=ds,
                out_table=tag_table,
                out_todo=root / "review_todo.csv",
                out_chars=root / "char_freq.csv",
                per_image_path=per_img,
                trigger="@myart",
            )
            sup = root / "supplement.json"
            common.atomic_write_json(sup, {
                "0001": {
                    "slots": {
                        "1": ["no humans"],
                        "2": ["white shirt", "pleated skirt"],
                        "4": ["outdoors"],
                        "5": ["day", "warm tone"],
                        "6": ["wide shot"],
                    },
                    "watermark": {"present": False, "tokens": []},
                },
            })
            overrides = root / "overrides.json"
            common.atomic_write_json(overrides, {
                "0001": {"add": ["soft_lighting"], "remove": []},
            })

            out_dir = root / "staged"
            rc_m = merge_captions.main([
                "--mode", "three-source",
                "--trigger", "@myart",
                "--dataset", str(ds),
                "--out-dir", str(out_dir),
                "--tag-table", str(tag_table),
                "--per-image", str(per_img),
                "--supplement", str(sup),
                "--overrides", str(overrides),
            ])
            self.assertEqual(rc_m, 0)
            cap = (out_dir / "0001.txt").read_text(encoding="utf-8")
            toks = [t.strip() for t in cap.split(",")]
            self.assertIn("white shirt", toks)
            self.assertIn("no humans", toks)
            self.assertIn("soft lighting", toks)
            self.assertNotIn("white_shirt", toks)
            self.assertNotIn("no_humans", toks)
            self.assertNotIn("soft_lighting", toks)
            self.assertEqual(len(toks), len(set(toks)))

            rc_v = validate_captions.main([
                "--captions-dir", str(out_dir),
                "--dataset", str(ds),
                "--trigger", "@myart",
                "--min-tags", "15",
            ])
            self.assertEqual(rc_v, 0)


class TestValidateCaptions(unittest.TestCase):
    def test_gate1_counts_format_neg_keep_and_conflict_exemptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ds = root / "ds"
            caps = root / "caps"
            ds.mkdir()
            caps.mkdir()

            filler = _make_filler_tags(20)
            # 0001: exactly 20 tags excl @myart (21 total incl trigger), includes no humans & no panties & no bra -> PASS
            Image.new("RGB", (32, 32)).save(ds / "0001.png")
            t20 = filler[:17] + ["no humans", "no panties", "no bra"]
            (caps / "0001.txt").write_bytes(("@myart, " + ", ".join(t20)).encode("utf-8"))

            # 0002: multi-person exemption + multicolored hair exemption + heterochromia exemption -> PASS
            Image.new("RGB", (32, 32)).save(ds / "0002.png")
            t_exempt = [
                "2girls", "yuri", "blonde hair", "blue hair", "multicolored hair",
                "blue eyes", "red eyes", "heterochromia",
            ] + filler[5:18]
            (caps / "0002.txt").write_bytes(("@myart, " + ", ".join(t_exempt)).encode("utf-8"))

            report_ok = root / "report_ok.json"
            rc = validate_captions.main([
                "--captions-dir", str(caps),
                "--dataset", str(ds),
                "--trigger", "@myart",
                "--min-tags", "20",
                "--max-tags", "45",
                "--out-report", str(report_ok),
            ])
            self.assertEqual(rc, 0)
            rep = json.loads(report_ok.read_text(encoding="utf-8"))
            self.assertEqual(rep["summary"]["total_offline_errors"], 0)
            self.assertEqual(rep["summary"]["total_warnings"], 0)
            self.assertEqual(rep["summary"]["tag_count_min"], 20)

            # 0003: BOM + trailing newline + leading comma + no cum + hair conflict + solo contradiction -> FAIL
            Image.new("RGB", (32, 32)).save(ds / "0003.png")
            (caps / "0003.txt").write_bytes(
                b"\xef\xbb\xbf, @myart, 1girl, solo, solo focus, blue hair, red hair, no cum, uncensored\n"
            )
            report_bad = root / "report_bad.json"
            rc_bad = validate_captions.main([
                "--captions-dir", str(caps),
                "--dataset", str(ds),
                "--trigger", "@myart",
                "--out-report", str(report_bad),
            ])
            self.assertEqual(rc_bad, 1)
            rep_bad = json.loads(report_bad.read_text(encoding="utf-8"))
            errs = rep_bad["errors"]
            self.assertIn("bom", errs)
            self.assertIn("trailing_newline", errs)
            self.assertIn("leading_comma", errs)
            self.assertIn("invalid_negative_tokens", errs)
            self.assertIn("banned_tokens", errs)
            self.assertIn("conflict_groups", errs)
            self.assertIn("people_contradiction", errs)

    def test_gate2_online_3state_cache_and_cowgirl_trap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            caps = root / "caps"
            caps.mkdir()
            (caps / "0001.txt").write_bytes(b"@myart, 1girl, solo, cowgirl, soft lighting")
            cache_db = root / "danbooru.sqlite"

            # 1. Network failure -> UNVERIFIED (not INVALID/ABSENT); --strict exits 1
            with mock.patch("common.http_get_json", side_effect=RuntimeError("proxy down")):
                rep_net = root / "rep_net.json"
                rc_strict = validate_captions.main([
                    "--captions-dir", str(caps),
                    "--trigger", "@myart",
                    "--online",
                    "--cache", str(cache_db),
                    "--strict",
                    "--out-report", str(rep_net),
                ])
            self.assertEqual(rc_strict, 1)
            d_net = json.loads(rep_net.read_text(encoding="utf-8"))
            self.assertEqual(d_net["summary"]["online_unverified"], 3)
            self.assertEqual(d_net["summary"]["online_invalid"], 0)
            self.assertEqual(d_net["online"]["results"]["1girl"]["status"], "UNVERIFIED")

            # 2. Online success + cowgirl semantic trap + SQLite cache hit on second run
            def fake_http(url: str, params: dict | None = None, **kwargs: object) -> object:
                if "tags.json" in url:
                    return [
                        {"name": "1girl", "category": 0, "post_count": 5000000, "is_deprecated": False},
                        {"name": "solo", "category": 0, "post_count": 4000000, "is_deprecated": False},
                        {"name": "cowgirl", "category": 0, "post_count": 0, "is_deprecated": False},
                    ]
                if "tag_aliases.json" in url:
                    return [{"antecedent_name": "cowgirl", "consequent_name": "cow_girl", "status": "active"}]
                return []

            rep_ok = root / "rep_online.json"
            with mock.patch("common.http_get_json", side_effect=fake_http) as mocked:
                rc_on = validate_captions.main([
                    "--captions-dir", str(caps),
                    "--trigger", "@myart",
                    "--min-tags", "1",
                    "--online",
                    "--cache", str(cache_db),
                    "--suggest-aliases",
                    "--out-report", str(rep_ok),
                ])
                first_calls = mocked.call_count
            self.assertEqual(rc_on, 1)  # cowgirl has post_count=0 -> INVALID
            self.assertGreaterEqual(first_calls, 1)
            d_on = json.loads(rep_ok.read_text(encoding="utf-8"))
            self.assertEqual(d_on["summary"]["online_valid"], 2)
            self.assertEqual(d_on["summary"]["online_invalid"], 1)
            self.assertTrue(d_on["online"]["alias_suggestions"]["cowgirl"]["semantic_trap"])

            # Second run should hit SQLite cache for tags.json (0 tags.json HTTP calls!)
            with mock.patch("common.http_get_json", side_effect=fake_http) as mocked2:
                validate_captions.main([
                    "--captions-dir", str(caps),
                    "--trigger", "@myart",
                    "--min-tags", "1",
                    "--online",
                    "--cache", str(cache_db),
                ])
                self.assertEqual(mocked2.call_count, 0, "Cached tags must skip network calls")


class TestApplyCaptionsAndEndToEnd(unittest.TestCase):
    def test_dry_run_backup_atomic_writeback_and_e2e(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ds = root / "my_style_ds"
            ds.mkdir()
            thumbs = root / "thumbs"
            desc = root / "desc"
            desc.mkdir()
            staged = root / "staged"
            backup_root = root / "backups"

            # Create 2 synthetic images + original drafts
            Image.new("RGBA", (2000, 1200), (10, 20, 30, 255)).save(ds / "0001.png")
            Image.new("RGB", (600, 600), (200, 100, 50)).save(ds / "0002.jpg")
            orig_draft_bytes = b", @myart, 1girl, original draft text\n"
            (ds / "0001.txt").write_bytes(orig_draft_bytes)
            (ds / "0002.txt").write_bytes(b", @myart, 1girl, draft two")

            # Stage 1: thumbs
            t_stats = make_thumbs.run_make_thumbs(ds, thumbs)
            self.assertEqual(t_stats["created"], 2)

            # Stage 1.5: tag table
            build_tag_table.run_build_tag_table(
                dataset=ds,
                out_table=root / "tag_table.csv",
                out_todo=root / "review_todo.csv",
                out_chars=root / "char_freq.csv",
                per_image_path=root / "per_image.json",
                trigger="@myart",
            )

            # Stage 2.5: parse descriptions
            filler = _make_filler_tags(22)
            for stem in ("0001", "0002"):
                (desc / f"{stem}.json").write_text(
                    json.dumps({
                        "PEOPLE": ["1girl", "solo"],
                        "HAIR": ["blonde hair", "long hair"],
                        "EYES": ["blue eyes"],
                        "EXPRESSION": ["smile", "looking at viewer", "blush"],
                        "BODY": ["small breasts", "bare shoulders"],
                        "CLOTHING": ["white dress", "frills", "ribbon", "shoes", "thighhighs"],
                        "POSE": ["standing", "arms up"],
                        "SCENE": ["indoors", "window", "curtains"],
                        "LIGHTING": ["day", "soft lighting", "warm tone"],
                        "COMPOSITION": ["upper body", "cowboy shot"],
                        "WATERMARK": ["none"],
                    }),
                    encoding="utf-8",
                )
            parse_descriptions.run_parse_descriptions(
                desc_dir=desc,
                out_json=root / "supplement.json",
                out_failed=root / "failed.txt",
                out_review=root / "review.txt",
                dataset=ds,
                thumbs_dir=thumbs,
            )

            # Stage 3: merge
            rc_m = merge_captions.main([
                "--mode", "two-source",
                "--trigger", "@myart",
                "--dataset", str(ds),
                "--out-dir", str(staged),
                "--supplement", str(root / "supplement.json"),
                "--tag-table", str(root / "tag_table.csv"),
            ])
            self.assertEqual(rc_m, 0)

            # Stage 4: validate
            val_report = root / "validation_report.json"
            rc_v = validate_captions.main([
                "--captions-dir", str(staged),
                "--dataset", str(ds),
                "--trigger", "@myart",
                "--watermark-ledger", str(root / "supplement.json"),
                "--out-report", str(val_report),
            ])
            self.assertEqual(rc_v, 0)

            # Stage 5a: Dry-run must NOT modify dataset or create backup
            summary_path = root / "caption_summary.json"
            rc_dry = apply_captions.main([
                "--staged-dir", str(staged),
                "--dataset", str(ds),
                "--backup-dir", str(backup_root),
                "--trigger", "@myart",
                "--validation-report", str(val_report),
                "--out-summary", str(summary_path),
            ])
            self.assertEqual(rc_dry, 0)
            self.assertEqual((ds / "0001.txt").read_bytes(), orig_draft_bytes)
            self.assertFalse(backup_root.exists())
            self.assertFalse(summary_path.exists())

            # Stage 5b: Blocked when online_unverified > 0 without --force-unverified
            unverified_rep = root / "unverified_report.json"
            rep_data = json.loads(val_report.read_text(encoding="utf-8"))
            rep_data["summary"]["online_unverified"] = 2
            unverified_rep.write_text(json.dumps(rep_data), encoding="utf-8")
            rc_block = apply_captions.main([
                "--staged-dir", str(staged),
                "--dataset", str(ds),
                "--backup-dir", str(backup_root),
                "--trigger", "@myart",
                "--validation-report", str(unverified_rep),
                "--out-summary", str(summary_path),
                "--apply",
            ])
            self.assertEqual(rc_block, 1)
            self.assertEqual((ds / "0001.txt").read_bytes(), orig_draft_bytes)

            # Stage 5c: Real --apply with valid report
            rc_app = apply_captions.main([
                "--staged-dir", str(staged),
                "--dataset", str(ds),
                "--backup-dir", str(backup_root),
                "--trigger", "@myart",
                "--target-type", "style",
                "--validation-report", str(val_report),
                "--out-summary", str(summary_path),
                "--apply",
            ])
            self.assertEqual(rc_app, 0)
            self.assertTrue(summary_path.is_file())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["trigger"], "@myart")
            self.assertEqual(summary["caption_count"], 2)
            self.assertTrue(summary["post_write_verified"])

            # Verify snapshot backup preserved exact pre-existing draft bytes
            bdir = Path(summary["backup_path"])
            self.assertEqual((bdir / "0001.txt").read_bytes(), orig_draft_bytes)

            # Verify written caption format (UTF-8 no BOM, no trailing newline)
            written = (ds / "0001.txt").read_bytes()
            self.assertFalse(written.startswith(b"\xef\xbb\xbf"))
            self.assertFalse(written.endswith(b"\n"))
            self.assertTrue(written.decode("utf-8").startswith("@myart, "))


if __name__ == "__main__":
    unittest.main()
