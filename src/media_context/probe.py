"""Zero-token local media probing using ffprobe / ffmpeg."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cache import CacheStore, estimate_media_tokens

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".heic"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts"}
ALL_MEDIA_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS


def classify_media(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return "other"


def _require_bin(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"Required binary '{name}' not found in PATH. Please install ffmpeg.")
    return found


def _parse_fps(rate_str: str) -> Optional[float]:
    if not rate_str or rate_str == "0/0":
        return None
    if "/" in rate_str:
        num, den = rate_str.split("/", 1)
        try:
            d = float(den)
            return round(float(num) / d, 3) if d else None
        except ValueError:
            return None
    try:
        return round(float(rate_str), 3)
    except ValueError:
        return None


def _detect_audio_levels(ffmpeg_bin: str, path: Path) -> Dict[str, Any]:
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "volumedetect,silencedetect=noise=-45dB:d=1.0",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    stderr = proc.stderr or ""
    out: Dict[str, Any] = {}
    m_mean = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", stderr)
    m_max = re.search(r"max_volume:\s*([-\d.]+)\s*dB", stderr)
    if m_mean:
        out["mean_volume_db"] = float(m_mean.group(1))
    if m_max:
        out["max_volume_db"] = float(m_max.group(1))
        out["clipping_risk"] = float(m_max.group(1)) >= -0.3
    silences = re.findall(r"silence_duration:\s*([\d.]+)", stderr)
    if silences:
        out["silence_segments"] = len(silences)
        out["total_silence_s"] = round(sum(float(x) for x in silences), 2)
    return out


def _detect_black_frames(ffmpeg_bin: str, path: Path) -> Dict[str, Any]:
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-vf",
        "blackdetect=d=0.5:pix_th=0.10",
        "-an",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    stderr = proc.stderr or ""
    blacks = re.findall(r"black_duration:([\d.]+)", stderr)
    return {
        "black_segments": len(blacks),
        "total_black_s": round(sum(float(x) for x in blacks), 2),
    }


def probe_media(
    path: str | Path,
    *,
    check_audio: bool = False,
    check_black: bool = False,
    store: Optional[CacheStore] = None,
) -> Dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")

    ffprobe_bin = _require_bin("ffprobe")
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(p),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()}")

    raw = json.loads(proc.stdout or "{}")
    fmt = raw.get("format", {})
    streams = raw.get("streams", [])

    size_bytes = p.stat().st_size
    kind = classify_media(p)
    result: Dict[str, Any] = {
        "path": str(p),
        "kind": kind,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 3),
        "format": fmt.get("format_name"),
    }
    if fmt.get("duration"):
        try:
            result["duration_s"] = round(float(fmt["duration"]), 3)
        except ValueError:
            pass
    if fmt.get("bit_rate"):
        try:
            result["bitrate_kbps"] = round(int(fmt["bit_rate"]) / 1000, 1)
        except ValueError:
            pass

    video_streams: List[Dict[str, Any]] = []
    audio_streams: List[Dict[str, Any]] = []
    for s in streams:
        ctype = s.get("codec_type")
        if ctype == "video":
            video_streams.append(
                {
                    "codec": s.get("codec_name"),
                    "width": s.get("width"),
                    "height": s.get("height"),
                    "fps": _parse_fps(s.get("avg_frame_rate") or s.get("r_frame_rate") or ""),
                    "pix_fmt": s.get("pix_fmt"),
                }
            )
        elif ctype == "audio":
            audio_streams.append(
                {
                    "codec": s.get("codec_name"),
                    "sample_rate": int(s["sample_rate"]) if s.get("sample_rate") else None,
                    "channels": s.get("channels"),
                    "channel_layout": s.get("channel_layout"),
                }
            )

    if video_streams:
        result["video"] = video_streams[0] if len(video_streams) == 1 else video_streams
    if audio_streams:
        result["audio"] = audio_streams[0] if len(audio_streams) == 1 else audio_streams

    if check_audio and audio_streams:
        result["audio_check"] = _detect_audio_levels(_require_bin("ffmpeg"), p)
    if check_black and video_streams and kind == "video":
        result["black_check"] = _detect_black_frames(_require_bin("ffmpeg"), p)

    out_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    if store is not None:
        raw_tokens = estimate_media_tokens(p)
        out_tokens = max(1, out_bytes // 4)
        store.record_saving("probe", size_bytes, out_bytes, max(0, raw_tokens - out_tokens), p.name)

    return result
