"""Stateless multimodal inspector backed by existing subscription CLIs with auto-fallback."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .cache import CacheStore, estimate_media_tokens, sha256_files
from .config import (
    SUPPORTED_BACKENDS,
    FallbackTarget,
    detect_available_backends,
    get_cooldown_seconds,
    resolve_fallback_chain,
)
from .media_ops import make_contact_sheet, shrink_image
from .probe import IMAGE_EXTS, VIDEO_EXTS, classify_media

_QUOTA_PATTERNS = re.compile(
    r"(429|quota|rate[ _-]?limit|limit reached|usage limit|exhausted|too many requests|credit|billing|resource_exhausted)",
    re.IGNORECASE,
)
_CAPACITY_PATTERNS = re.compile(
    r"(503|no capacity|overloaded|unavailable|service unavailable|server overloaded|temporarily unavailable|502|504)",
    re.IGNORECASE,
)
_AUTH_PATTERNS = re.compile(
    r"(401|unauthenticated|not logged in|invalid authentication|token expired|login required)",
    re.IGNORECASE,
)


def classify_failure(error_text: str) -> Tuple[str, bool]:
    """Classify worker failure into (category, should_fallback)."""
    if _QUOTA_PATTERNS.search(error_text):
        return "quota_exhausted", True
    if _CAPACITY_PATTERNS.search(error_text):
        return "capacity_unavailable", True
    if _AUTH_PATTERNS.search(error_text):
        return "auth_error", True
    if "timed out" in error_text.lower() or "timeout" in error_text.lower():
        return "timeout", True
    return "runtime_error", True


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


def _default_runner(cmd: List[str], timeout_s: int, env: Dict[str, str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, env=env)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"Worker timed out after {timeout_s}s"


def inspect_media(
    inputs: List[str | Path],
    prompt: str,
    *,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    grid: str = "3x3",
    max_side: int = 1024,
    no_cache: bool = False,
    no_fallback: bool = False,
    timeout_s: int = 180,
    store: Optional[CacheStore] = None,
    runner: Optional[Callable[[List[str], int, Dict[str, str]], Tuple[int, str, str]]] = None,
) -> Dict[str, Any]:
    """Run an isolated, stateless inspection via subscription CLI with automatic quota/capacity fallback."""
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

    chain = resolve_fallback_chain(preferred_backend=backend, preferred_model=model)
    # Filter to installed CLIs unless a custom test runner is supplied
    if runner is None:
        chain = [t for t in chain if shutil.which(t.backend)]
    if no_fallback and chain:
        chain = chain[:1]
    if not chain:
        raise RuntimeError(
            "No supported subscription CLI found in PATH. "
            f"Install one of: {', '.join(SUPPORTED_BACKENDS)}."
        )

    cache = store or CacheStore()
    fhash = sha256_files(resolved)
    src_bytes = sum(p.stat().st_size for p in resolved)
    raw_tokens = sum(estimate_media_tokens(p) for p in resolved)

    # Check cache across active chain targets (primary first)
    if not no_cache:
        for t in chain:
            ck = cache.make_key(fhash, t.backend, t.model, prompt)
            hit = cache.get_inspect(ck)
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
                    "model": hit["model"] or "default",
                    "cached": True,
                    "cache_hits": hit["hits"],
                    "elapsed_s": 0.0,
                    "fallback_used": False,
                    "fallback_notice": "",
                    "result": hit["result"],
                    "source_count": len(resolved),
                }

    prepared = _prepare_visual_inputs(resolved, grid=grid, max_side=max_side)
    env = os.environ.copy()
    env["MCTX_BYPASS"] = "1"
    exec_fn = runner or _default_runner
    cooldown_s = get_cooldown_seconds()

    attempts: List[Dict[str, Any]] = []
    primary = chain[0]

    for idx, target in enumerate(chain):
        cd = cache.get_cooldown(target.key)
        if cd and len(chain) > 1:
            attempts.append(
                {
                    "target": target.key,
                    "status": "skipped_cooldown",
                    "reason": cd["reason"],
                    "remaining_s": cd["remaining_s"],
                }
            )
            continue

        cmd = _build_worker_command(target.backend, prepared, prompt, model=target.model or None)
        t0 = time.monotonic()
        rc, stdout, stderr = exec_fn(cmd, timeout_s, env)
        elapsed = round(time.monotonic() - t0, 2)

        if rc == 0 and stdout.strip():
            answer = stdout.strip()
            ck = cache.make_key(fhash, target.backend, target.model, prompt)
            cache.put_inspect(ck, fhash, target.backend, target.model, prompt, answer)

            out_bytes = len(answer.encode("utf-8"))
            cache.record_saving(
                "inspect",
                src_bytes,
                out_bytes,
                max(0, raw_tokens - max(1, out_bytes // 4)),
                f"{target.key} ({len(resolved)} file(s))",
            )

            fallback_used = target.key != primary.key
            notice = ""
            if fallback_used:
                prev_reasons = "; ".join(f"{a['target']} ({a['reason']})" for a in attempts)
                notice = f"Fallback triggered: {prev_reasons} -> switched to {target.key}"

            return {
                "backend": target.backend,
                "model": target.model or "default",
                "target": target.key,
                "cached": False,
                "elapsed_s": elapsed,
                "fallback_used": fallback_used,
                "fallback_notice": notice,
                "attempts": attempts,
                "prepared_files": [str(x) for x in prepared],
                "result": answer,
                "source_count": len(resolved),
            }

        err_msg = (stderr or stdout or f"exit {rc}").strip()
        category, can_fallback = classify_failure(err_msg)
        attempts.append(
            {
                "target": target.key,
                "status": "failed",
                "category": category,
                "reason": f"{category}: {err_msg[:180]}",
                "elapsed_s": elapsed,
            }
        )
        if category in ("quota_exhausted", "capacity_unavailable", "auth_error"):
            cache.set_cooldown(target.key, category, cooldown_s)
        if not can_fallback or idx == len(chain) - 1:
            break

    summary = "; ".join(f"{a['target']} -> {a.get('reason', a['status'])}" for a in attempts)
    raise RuntimeError(
        f"All inspection backends exhausted/failed: {summary}. "
        "Tip: check quota/login or run `mctx config reset-cooldown`."
    )
