"""Local visual compression: contact sheet stitching and image shrinking via ffmpeg."""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .cache import CacheStore, estimate_media_tokens, get_artifacts_dir, sha256_file, sha256_files
from .probe import IMAGE_EXTS, VIDEO_EXTS, classify_media, probe_media


def _require_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("Required binary 'ffmpeg' not found in PATH.")
    return found


def shrink_image(
    path: str | Path,
    *,
    max_side: int = 1024,
    quality: int = 78,
    output: Optional[str | Path] = None,
    store: Optional[CacheStore] = None,
) -> Dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Image not found: {p}")

    ffmpeg = _require_ffmpeg()
    fhash = sha256_file(p)[:12]
    if output:
        out_path = Path(output).expanduser().resolve()
    else:
        out_path = get_artifacts_dir() / f"mctx_shrink_{p.stem}_{fhash}.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Map JPEG quality 1..100 -> ffmpeg q:v 31..2
    qv = max(2, min(31, int(round(31 - (quality / 100.0) * 29))))
    vf = f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease"
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(p),
        "-vf",
        vf,
        "-q:v",
        str(qv),
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg shrink failed: {proc.stderr.strip()}")

    src_bytes = p.stat().st_size
    out_bytes = out_path.stat().st_size
    saved_tokens = max(0, estimate_media_tokens(p) - estimate_media_tokens(out_path))
    if store is not None:
        store.record_saving("shrink", src_bytes, out_bytes, saved_tokens, p.name)

    return {
        "source": str(p),
        "output": str(out_path),
        "source_bytes": src_bytes,
        "output_bytes": out_bytes,
        "reduction_pct": round((1 - out_bytes / src_bytes) * 100, 1) if src_bytes else 0.0,
    }


def _parse_grid(grid: str) -> Tuple[int, int]:
    m = grid.lower().split("x")
    if len(m) != 2:
        raise ValueError(f"Invalid grid '{grid}'. Expected format like '3x3' or '4x3'.")
    return max(1, int(m[0])), max(1, int(m[1]))


def make_contact_sheet(
    inputs: List[str | Path],
    *,
    grid: str = "3x3",
    width: int = 1280,
    quality: int = 78,
    timestamps: bool = True,
    output: Optional[str | Path] = None,
    store: Optional[CacheStore] = None,
) -> Dict[str, Any]:
    """Create a single compressed contact sheet JPEG from a video or multiple image frames."""
    if not inputs:
        raise ValueError("No inputs provided for contact sheet.")

    resolved: List[Path] = []
    for raw in inputs:
        p = Path(raw).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"Input not found: {p}")
        if p.is_dir():
            imgs = sorted(x for x in p.iterdir() if x.is_file() and x.suffix.lower() in IMAGE_EXTS)
            resolved.extend(imgs)
        else:
            resolved.append(p)

    if not resolved:
        raise ValueError("No media files found in provided inputs.")

    cols, rows = _parse_grid(grid)
    max_tiles = cols * rows
    tile_w = max(160, width // cols)
    qv = max(2, min(31, int(round(31 - (quality / 100.0) * 29))))
    ffmpeg = _require_ffmpeg()

    # Case 1: Single video input -> sample evenly across duration and tile directly
    if len(resolved) == 1 and resolved[0].suffix.lower() in VIDEO_EXTS:
        video = resolved[0]
        info = probe_media(video)
        dur = float(info.get("duration_s") or 10.0)
        fps_expr = max(0.05, max_tiles / max(dur, 0.5))

        fhash = sha256_file(video)[:12]
        out_path = (
            Path(output).expanduser().resolve()
            if output
            else get_artifacts_dir() / f"mctx_sheet_{video.stem}_{cols}x{rows}_{fhash}.jpg"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        filters = [f"fps={fps_expr:.5f}", f"scale={tile_w}:-2"]
        if timestamps:
            filters.append(
                "drawtext=text='%{pts\\:hms}':x=8:y=8:fontsize=18:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=4"
            )
        filters.append(f"tile={cols}x{rows}:padding=4:margin=4:color=black")

        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            ",".join(filters),
            "-frames:v",
            "1",
            "-q:v",
            str(qv),
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 and timestamps:
            # Fallback if ffmpeg lacks drawtext/libfreetype
            filters_no_txt = [f"fps={fps_expr:.5f}", f"scale={tile_w}:-2", f"tile={cols}x{rows}:padding=4:margin=4:color=black"]
            cmd[cmd.index("-vf") + 1] = ",".join(filters_no_txt)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg contact sheet failed: {proc.stderr.strip()}")

        src_bytes = video.stat().st_size
        out_bytes = out_path.stat().st_size
        saved_tokens = max(0, estimate_media_tokens(video) - estimate_media_tokens(out_path))
        if store is not None:
            store.record_saving("sheet_video", src_bytes, out_bytes, saved_tokens, video.name)

        return {
            "mode": "video",
            "source": str(video),
            "output": str(out_path),
            "grid": f"{cols}x{rows}",
            "duration_s": dur,
            "source_bytes": src_bytes,
            "output_bytes": out_bytes,
        }

    # Case 2: Multiple images -> uniformly sample up to cols*rows and stitch
    images = [p for p in resolved if classify_media(p) == "image"]
    if not images:
        raise ValueError("Expected either 1 video file or a list/directory of image files.")

    if len(images) > max_tiles:
        step = len(images) / max_tiles
        selected = [images[min(len(images) - 1, int(math.floor(i * step)))] for i in range(max_tiles)]
    else:
        selected = images
        cols = min(cols, len(selected))
        rows = math.ceil(len(selected) / cols)
        tile_w = max(160, width // cols)

    fhash = sha256_files(selected)[:12]
    out_path = (
        Path(output).expanduser().resolve()
        if output
        else get_artifacts_dir() / f"mctx_sheet_imgs_{len(selected)}_{cols}x{rows}_{fhash}.jpg"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mctx_sheet_") as tmp:
        tmp_dir = Path(tmp)
        # Normalize each image to uniform size via ffmpeg, then tile
        norm_files: List[Path] = []
        for idx, img in enumerate(selected):
            nf = tmp_dir / f"f_{idx:04d}.jpg"
            cmd_norm = [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(img),
                "-vf",
                f"scale={tile_w}:{int(tile_w * 9 / 16)}:force_original_aspect_ratio=decrease,pad={tile_w}:{int(tile_w * 9 / 16)}:(ow-iw)/2:(oh-ih)/2:black",
                "-q:v",
                "4",
                str(nf),
            ]
            p_norm = subprocess.run(cmd_norm, capture_output=True, text=True, timeout=30)
            if p_norm.returncode != 0:
                raise RuntimeError(f"Failed normalizing {img}: {p_norm.stderr.strip()}")
            norm_files.append(nf)

        cmd_tile = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            "1",
            "-i",
            str(tmp_dir / "f_%04d.jpg"),
            "-vf",
            f"tile={cols}x{rows}:padding=4:margin=4:color=black",
            "-frames:v",
            "1",
            "-q:v",
            str(qv),
            str(out_path),
        ]
        p_tile = subprocess.run(cmd_tile, capture_output=True, text=True, timeout=60)
        if p_tile.returncode != 0:
            raise RuntimeError(f"Failed tiling images: {p_tile.stderr.strip()}")

    src_bytes = sum(p.stat().st_size for p in images)
    out_bytes = out_path.stat().st_size
    raw_tokens = sum(estimate_media_tokens(p) for p in images)
    saved_tokens = max(0, raw_tokens - estimate_media_tokens(out_path))
    if store is not None:
        store.record_saving("sheet_images", src_bytes, out_bytes, saved_tokens, f"{len(images)} images -> 1 sheet")

    return {
        "mode": "images",
        "source_count": len(images),
        "sampled_count": len(selected),
        "output": str(out_path),
        "grid": f"{cols}x{rows}",
        "source_bytes": src_bytes,
        "output_bytes": out_bytes,
        "reduction_pct": round((1 - out_bytes / src_bytes) * 100, 1) if src_bytes else 0.0,
    }
