"""Downsample dataset images to JPEG thumbnails (<=1536px long edge, <=2MB)."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import iter_images  # noqa: E402

Image.MAX_IMAGE_PIXELS = None


def make_thumbnail(
    src_path: Path,
    dst_path: Path,
    long_edge: int = 1536,
    max_mb: float = 2.0,
    quality: int = 85,
) -> Path:
    """Create a JPEG thumbnail for src_path at dst_path with size & edge limits."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(max_mb * 1024 * 1024)

    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")

        w, h = im.size
        if max(w, h) > long_edge:
            scale = long_edge / max(w, h)
            new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            im = im.resize(new_size, resample)

        q = quality
        while True:
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=q, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes or q <= 30:
                dst_path.write_bytes(data)
                break
            q = max(30, q - 10)

    return dst_path


def run_make_thumbs(
    src: Path,
    dst: Path,
    long_edge: int = 1536,
    max_mb: float = 2.0,
    quality: int = 85,
    recursive: bool = False,
    force: bool = False,
    limit: int = 0,
) -> dict[str, int]:
    """Generate thumbnails for all images in src into dst."""
    if not src.is_dir():
        raise FileNotFoundError(f"Source directory not found: {src}")
    dst.mkdir(parents=True, exist_ok=True)

    images = iter_images(src, recursive=recursive)
    if limit > 0:
        images = images[:limit]

    ok = skip = fail = 0
    for img_path in images:
        out_path = dst / f"{img_path.stem}.jpg"
        if out_path.exists() and not force:
            skip += 1
            continue
        try:
            make_thumbnail(img_path, out_path, long_edge=long_edge, max_mb=max_mb, quality=quality)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"[FAIL] {img_path.name}: {exc}", file=sys.stderr)

    return {"total": len(images), "created": ok, "skipped": skip, "failed": fail}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create downsampled JPEG thumbnails for vision inspection.")
    ap.add_argument("--src", required=True, type=Path, help="Source image directory")
    ap.add_argument("--dst", required=True, type=Path, help="Destination thumbnail directory")
    ap.add_argument("--long-edge", type=int, default=1536, help="Max long edge in pixels (default: 1536)")
    ap.add_argument("--max-mb", type=float, default=2.0, help="Max output file size in MB (default: 2.0)")
    ap.add_argument("--quality", type=int, default=85, help="Initial JPEG quality (default: 85)")
    ap.add_argument("--recursive", action="store_true", help="Scan subdirectories recursively")
    ap.add_argument("--force", action="store_true", help="Overwrite existing thumbnails")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N images (0 = all)")
    args = ap.parse_args(argv)

    stats = run_make_thumbs(
        src=args.src,
        dst=args.dst,
        long_edge=args.long_edge,
        max_mb=args.max_mb,
        quality=args.quality,
        recursive=args.recursive,
        force=args.force,
        limit=args.limit,
    )
    print(
        f"Thumbnails complete: created={stats['created']} skipped={stats['skipped']} "
        f"failed={stats['failed']} total={stats['total']} -> {args.dst}"
    )
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
