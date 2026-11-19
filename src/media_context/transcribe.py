"""Zero-cost local audio transcription wrapper (auto-detects installed local whisper engines)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .cache import CacheStore, estimate_media_tokens


def detect_transcribe_engine() -> Optional[str]:
    for cand in ("mlx_whisper", "whisper-cli", "whisper"):
        if shutil.which(cand):
            return cand
    return None


def transcribe_audio(
    path: str | Path,
    *,
    language: Optional[str] = None,
    model: Optional[str] = None,
    store: Optional[CacheStore] = None,
) -> Dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Audio/video file not found: {p}")

    engine = detect_transcribe_engine()
    if not engine:
        raise RuntimeError(
            "No local offline transcription engine found in PATH. "
            "Install one of: `pipx install mlx-whisper` (Apple Silicon), "
            "`brew install whisper-cpp` (whisper-cli), or `pipx install openai-whisper`."
        )

    with tempfile.TemporaryDirectory(prefix="mctx_tr_") as tmp:
        tmp_dir = Path(tmp)
        if engine == "mlx_whisper":
            cmd = [engine, str(p), "--output-dir", str(tmp_dir), "--output-format", "txt"]
            if language:
                cmd += ["--language", language]
            if model:
                cmd += ["--model", model]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise RuntimeError(f"mlx_whisper failed: {proc.stderr.strip()}")
            txts = list(tmp_dir.glob("*.txt"))
            text = txts[0].read_text(encoding="utf-8", errors="replace").strip() if txts else proc.stdout.strip()
        elif engine == "whisper-cli":
            cmd = [engine, "-f", str(p), "-nt"]
            if language:
                cmd += ["-l", language]
            if model:
                cmd += ["-m", model]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise RuntimeError(f"whisper-cli failed: {proc.stderr.strip()}")
            text = proc.stdout.strip()
        else:  # openai-whisper CLI
            cmd = [engine, str(p), "--output_dir", str(tmp_dir), "--output_format", "txt"]
            if language:
                cmd += ["--language", language]
            if model:
                cmd += ["--model", model]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise RuntimeError(f"whisper failed: {proc.stderr.strip()}")
            txts = list(tmp_dir.glob("*.txt"))
            text = txts[0].read_text(encoding="utf-8", errors="replace").strip() if txts else proc.stdout.strip()

    src_bytes = p.stat().st_size
    out_bytes = len(text.encode("utf-8"))
    if store is not None:
        saved_tokens = max(0, estimate_media_tokens(p) - max(1, out_bytes // 4))
        store.record_saving("transcribe", src_bytes, out_bytes, saved_tokens, p.name)

    return {
        "source": str(p),
        "engine": engine,
        "text": text,
        "source_bytes": src_bytes,
        "output_bytes": out_bytes,
    }
