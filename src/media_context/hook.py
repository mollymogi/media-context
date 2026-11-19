"""Cross-client PreToolUse hook for Antigravity, Claude Code, and OpenCode."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from .cache import CacheStore, estimate_media_tokens, get_artifacts_dir
from .media_ops import shrink_image
from .probe import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS

WHITELIST_PREFIXES = ("mctx_sheet_", "mctx_shrink_", "contact_sheet_")


def _extract_file_path(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Return (field_name, path_str) from Antigravity/Claude/OpenCode PreToolUse payloads."""
    tool_input = payload.get("tool_input") or payload.get("input") or payload.get("arguments") or {}
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            return "", ""
    if not isinstance(tool_input, dict):
        return "", ""
    for key in ("Path", "file_path", "path", "filePath"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return key, val.strip()
    return "", ""


def _is_whitelisted(p: Path) -> bool:
    name = p.name.lower()
    if any(name.startswith(pref) for pref in WHITELIST_PREFIXES):
        return True
    try:
        art_dir = get_artifacts_dir().resolve()
        if art_dir in p.resolve().parents:
            return True
    except Exception:
        pass
    return False


def evaluate_hook(
    payload: Dict[str, Any],
    *,
    client: str = "generic",
    mode: str = "block",
    max_image_bytes: int = 350_000,
    max_side: int = 1024,
) -> Tuple[int, str, str]:
    """Evaluate a PreToolUse hook payload.

    Returns: (exit_code, stdout_text, stderr_text)
    - exit_code 0: allow (stdout may contain rewritten arguments if supported)
    - exit_code 2: block tool call and feed stderr guidance back to the agent
    """
    if os.environ.get("MCTX_BYPASS") == "1":
        return 0, "", ""

    field_name, raw_path = _extract_file_path(payload)
    if not raw_path:
        return 0, "", ""

    p = Path(raw_path).expanduser()
    ext = p.suffix.lower()
    if ext not in (IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS):
        return 0, "", ""

    if _is_whitelisted(p):
        return 0, "", ""

    store = CacheStore()
    size_bytes = p.stat().st_size if p.exists() else 0
    est_tokens = estimate_media_tokens(p) if p.exists() else 2000

    # Audio & Video: always block raw read_file to protect context window
    if ext in AUDIO_EXTS:
        store.record_saving("hook_block_audio", size_bytes, 0, est_tokens, p.name)
        msg = (
            f"[media-context] Blocked direct Read of audio file '{p.name}' "
            f"(~{est_tokens} tokens saved). Use instead:\n"
            f"  - Metadata & silence/volume check: mctx probe \"{p}\" --audio\n"
            f"  - Local free transcription:        mctx transcribe \"{p}\"\n"
            f"  - Isolated stateless inspection:   mctx inspect \"{p}\" -q \"<your question>\""
        )
        return 2, "", msg

    if ext in VIDEO_EXTS:
        store.record_saving("hook_block_video", size_bytes, 0, est_tokens, p.name)
        msg = (
            f"[media-context] Blocked direct Read of video file '{p.name}' "
            f"(~{est_tokens} tokens saved). Use instead:\n"
            f"  - Metadata & black-frame check:    mctx probe \"{p}\" --black\n"
            f"  - Generate 1 contact sheet:        mctx sheet \"{p}\" --grid 3x3\n"
            f"  - Isolated stateless inspection:   mctx inspect \"{p}\" -q \"<your question>\""
        )
        return 2, "", msg

    # Image handling
    if mode == "shrink" and p.exists():
        if size_bytes <= max_image_bytes:
            return 0, "", ""
        res = shrink_image(p, max_side=max_side, store=store)
        shrunk_path = res["output"]
        if client in ("claude", "antigravity"):
            out_obj = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {field_name: shrunk_path},
                }
            }
            return 0, json.dumps(out_obj, ensure_ascii=False), ""
        # Fallback: instruct agent to read shrunk artifact
        msg = (
            f"[media-context] Original image '{p.name}' is {size_bytes // 1024}KB. "
            f"Compressed copy created ({res['output_bytes'] // 1024}KB, -{res['reduction_pct']}%). "
            f"Read this lightweight copy instead: {shrunk_path}"
        )
        return 2, "", msg

    # Default 'block' mode for images
    store.record_saving("hook_block_image", size_bytes, 0, est_tokens, p.name)
    msg = (
        f"[media-context] Blocked direct Read of raw image '{p.name}' "
        f"(~{est_tokens} tokens saved) to prevent main-session context bloat. Use instead:\n"
        f"  - Isolated stateless inspection (0 context leak): mctx inspect \"{p}\" -q \"<your question>\"\n"
        f"  - Stitch multiple frames into 1 contact sheet:    mctx sheet <dir_or_files> --grid 3x3\n"
        f"  - Or create a lightweight copy to read:           mctx shrink \"{p}\""
    )
    return 2, "", msg


def run_hook_stdio(client: str = "generic", mode: str = "block", max_side: int = 1024) -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    code, out, err = evaluate_hook(payload, client=client, mode=mode, max_side=max_side)
    if out:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    if err:
        sys.stderr.write(err + "\n")
        sys.stderr.flush()
    return code


def render_hook_config(client: str, mode: str = "block") -> Dict[str, Any]:
    """Return ready-to-paste hook configuration snippet for the requested client."""
    cmd = f"mctx hook run --client {client} --mode {mode}"
    if client == "antigravity":
        return {
            "media-context-guard": {
                "enabled": True,
                "PreToolUse": [
                    {
                        "matcher": "read_file",
                        "hooks": [{"type": "command", "command": cmd, "timeout": 15}],
                    }
                ],
            }
        }
    if client == "claude":
        return {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Read",
                        "hooks": [{"type": "command", "command": cmd}],
                    }
                ]
            }
        }
    return {
        "PreToolUse": [
            {
                "matcher": "read_file|Read",
                "command": cmd,
            }
        ]
    }
