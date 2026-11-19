"""Stateless multimodal inspector backed by existing subscription CLIs (zero extra API cost)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cache import CacheStore, estimate_media_tokens, sha256_files
from .media_ops import make_contact_sheet, shrink_image
from .probe import IMAGE_EXTS, VIDEO_EXTS, classify_media

SUPPORTED_BACKENDS = ("agy", "claude", "codex", "opencode")


def detect_available_backends() -> List[str]:
    return [b for b in SUPPORTED_BACKENDS if shutil.which(b)]


def resolve_backend(preferred: Optional[str] = None) -> str:
    env_backend = os.environ.get("MCTX_BACKEND", "").strip().lower()
    choice = (preferred or env_backend or "auto").lower()
    if choice != "auto":
        if choice not in SUPPORTED_BACKENDS:
            raise ValueError(f"Unsupported backend '{choice}'. Choose from: {', '.join(SUPPORTED_BACKENDS)}")
        if not shutil.which(choice):
            raise RuntimeError(f"Requested backend '{choice}' is not installed in PATH.")
        return choice

    avail = detect_available_backends()
    if not avail:
        raise RuntimeError(
            "No supported subscription CLI found in PATH. "
            "Install one of: agy, claude, codex, opencode."
        )
    return avail[0]


def _prepare_visual_inputs(paths: List[Path], grid: str, max_side: int) -> List[Path]:
    """Compress raw video or multiple images into lightweight visual artifact(s) for the stateless worker."""
    videos = [p for p in paths if p.suffix.lower() in VIDEO_EXTS]
    images = [p for p in paths if p.suffix.lower() in IMAGE_EXTS]
    others = [p for p in paths if p not in videos and p not in images]

    prepared: List[Path] = []
    for v in videos:
        res = make_contact_sheet([v], grid=grid, width=1280)
        prepared.append(Path(res["output"]))

    if len(images) > 2:
        res = make_contact_sheet(images, grid=grid, width=1280)
        prepared.append(Path(res["output"]))
    else:
        for img in images:
            if img.stat().st_size > 350_000:
                res = shrink_image(img, max_side=max_side)
                prepared.append(Path(res["output"]))
            else:
                prepared.append(img)

    prepared.extend(others)
    return prepared


def _build_worker_command(
    backend: str,
    files: List[Path],
    prompt: str,
    model: Optional[str] = None,
) -> List[str]:
    file_list = "\n".join(f"- {f}" for f in files)
    full_prompt = (
        "You are a stateless media inspection worker. Inspect the following local file(s) "
        "and answer concisely in plain text or compact markdown. Do not ask follow-up questions.\n\n"
        f"Files:\n{file_list}\n\n"
        f"Task: {prompt.strip()}"
    )

    dirs = sorted({str(f.parent) for f in files})

    if backend == "agy":
        cmd = ["agy", "-p", "--disable-slash-commands", "--output-format", "text", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        for d in dirs:
            cmd += ["--add-dir", d]
        cmd.append(full_prompt)
        return cmd

    if backend == "claude":
        cmd = ["claude", "-p", "--allowedTools", "Read"]
        if model:
            cmd += ["--model", model]
        for d in dirs:
            cmd += ["--add-dir", d]
        cmd.append(full_prompt)
        return cmd

    if backend == "codex":
        cmd = ["codex", "exec"]
        if model:
            cmd += ["-m", model]
        for f in files:
            if classify_media(f) == "image":
                cmd += ["-i", str(f)]
        cmd.append(full_prompt)
        return cmd

    if backend == "opencode":
        cmd = ["opencode", "run"]
        if model:
            cmd += ["-m", model]
        for f in files:
            cmd += ["-f", str(f)]
        cmd.append(full_prompt)
        return cmd

    raise ValueError(f"Unsupported backend: {backend}")


def inspect_media(
    inputs: List[str | Path],
    prompt: str,
    *,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    grid: str = "3x3",
    max_side: int = 1024,
    no_cache: bool = False,
    timeout_s: int = 180,
    store: Optional[CacheStore] = None,
) -> Dict[str, Any]:
    """Run an isolated, stateless inspection via an existing subscription CLI and return concise text."""
    resolved: List[Path] = []
    for raw in inputs:
        p = Path(raw).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"Input not found: {p}")
        if p.is_dir():
            resolved.extend(sorted(x for x in p.iterdir() if x.is_file() and classify_media(x) != "other"))
        else:
            resolved.append(p)

    if not resolved:
        raise ValueError("No media files found to inspect.")

    chosen_backend = resolve_backend(backend)
    chosen_model = model or os.environ.get("MCTX_MODEL", "")
    cache = store or CacheStore()
    fhash = sha256_files(resolved)
    cache_key = cache.make_key(fhash, chosen_backend, chosen_model, prompt)

    src_bytes = sum(p.stat().st_size for p in resolved)
    raw_tokens = sum(estimate_media_tokens(p) for p in resolved)

    if not no_cache:
        hit = cache.get_inspect(cache_key)
        if hit:
            out_bytes = len(hit["result"].encode("utf-8"))
            cache.record_saving(
                "inspect_cache_hit",
                src_bytes,
                out_bytes,
                max(0, raw_tokens - max(1, out_bytes // 4)),
                f"{len(resolved)} file(s)",
            )
            return {
                "backend": hit["backend"],
                "model": hit["model"],
                "cached": True,
                "cache_hits": hit["hits"],
                "elapsed_s": 0.0,
                "result": hit["result"],
                "source_count": len(resolved),
            }

    prepared = _prepare_visual_inputs(resolved, grid=grid, max_side=max_side)
    cmd = _build_worker_command(chosen_backend, prepared, prompt, model=chosen_model or None)

    env = os.environ.copy()
    # Critical: prevent our own PreToolUse hook from blocking this stateless worker!
    env["MCTX_BYPASS"] = "1"

    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, env=env)
    elapsed = round(time.monotonic() - t0, 2)

    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Backend '{chosen_backend}' failed (exit {proc.returncode}): {err_msg[:500]}")

    answer = (proc.stdout or "").strip()
    cache.put_inspect(cache_key, fhash, chosen_backend, chosen_model, prompt, answer)

    out_bytes = len(answer.encode("utf-8"))
    cache.record_saving(
        "inspect",
        src_bytes,
        out_bytes,
        max(0, raw_tokens - max(1, out_bytes // 4)),
        f"{chosen_backend} ({len(resolved)} file(s))",
    )

    return {
        "backend": chosen_backend,
        "model": chosen_model or "default",
        "cached": False,
        "elapsed_s": elapsed,
        "prepared_files": [str(x) for x in prepared],
        "result": answer,
        "source_count": len(resolved),
    }
