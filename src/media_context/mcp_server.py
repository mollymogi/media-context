"""Zero-dependency stdio JSON-RPC 2.0 MCP Server for media-context."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

from . import __version__
from .cache import CacheStore
from .inspector import inspect_media
from .media_ops import make_contact_sheet, shrink_image
from .probe import probe_media
from .transcribe import transcribe_audio

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "media_probe",
        "description": (
            "Zero-token local media probe via ffprobe/ffmpeg. Returns compact JSON with duration, "
            "resolution, fps, codecs, and optional audio volume/silence or video black-frame checks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to image, audio, or video file."},
                "check_audio": {"type": "boolean", "description": "Run volumedetect & silencedetect."},
                "check_black": {"type": "boolean", "description": "Run blackdetect on video."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "media_sheet",
        "description": (
            "Compress a video or multiple image frames into a single low-resolution timestamped "
            "contact sheet JPEG to save 85%+ visual tokens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "inputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "1 video file path, or a list of image file paths / directories.",
                },
                "grid": {"type": "string", "description": "Grid layout like '3x3' or '4x3'. Default '3x3'."},
                "width": {"type": "integer", "description": "Total sheet width in pixels. Default 1280."},
                "timestamps": {"type": "boolean", "description": "Overlay timestamps on video tiles. Default true."},
            },
            "required": ["inputs"],
        },
    },
    {
        "name": "media_shrink",
        "description": "Downscale and compress a single large image into a lightweight JPEG copy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to image file."},
                "max_side": {"type": "integer", "description": "Max width/height in pixels. Default 1024."},
                "quality": {"type": "integer", "description": "JPEG quality 1-100. Default 78."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "media_inspect",
        "description": (
            "Inspect images/videos/audio in an isolated, stateless subscription CLI worker "
            "(zero extra API cost, zero media tokens leaked into the main conversation). "
            "Automatically creates contact sheets for videos/multi-images and caches results in SQLite."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "inputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of media file paths or directories to inspect.",
                },
                "question": {"type": "string", "description": "Concise inspection question or checklist."},
                "backend": {
                    "type": "string",
                    "description": "Optional backend CLI: auto, agy, claude, codex, opencode.",
                },
                "model": {"type": "string", "description": "Optional model override for the worker CLI."},
                "grid": {"type": "string", "description": "Contact sheet grid for videos/multi-images. Default '3x3'."},
                "no_cache": {"type": "boolean", "description": "Bypass SQLite cache."},
            },
            "required": ["inputs", "question"],
        },
    },
    {
        "name": "media_transcribe",
        "description": "Zero-cost local offline audio/video transcription using installed whisper engine.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to audio or video file."},
                "language": {"type": "string", "description": "Optional language code (e.g. 'zh', 'en')."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "media_stats",
        "description": "Return cumulative bytes and estimated tokens saved by media-context.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _handle_tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    store = CacheStore()
    if name == "media_probe":
        res = probe_media(
            args["path"],
            check_audio=bool(args.get("check_audio", False)),
            check_black=bool(args.get("check_black", False)),
            store=store,
        )
    elif name == "media_sheet":
        res = make_contact_sheet(
            args["inputs"],
            grid=str(args.get("grid", "3x3")),
            width=int(args.get("width", 1280)),
            timestamps=bool(args.get("timestamps", True)),
            store=store,
        )
    elif name == "media_shrink":
        res = shrink_image(
            args["path"],
            max_side=int(args.get("max_side", 1024)),
            quality=int(args.get("quality", 78)),
            store=store,
        )
    elif name == "media_inspect":
        res = inspect_media(
            args["inputs"],
            args["question"],
            backend=args.get("backend"),
            model=args.get("model"),
            grid=str(args.get("grid", "3x3")),
            no_cache=bool(args.get("no_cache", False)),
            store=store,
        )
    elif name == "media_transcribe":
        res = transcribe_audio(
            args["path"],
            language=args.get("language"),
            store=store,
        )
    elif name == "media_stats":
        res = store.get_stats()
    else:
        raise ValueError(f"Unknown tool: {name}")
    return res


def handle_rpc_message(msg: Dict[str, Any]) -> Dict[str, Any] | None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications have no id and expect no response
    if req_id is None and method.startswith("notifications/"):
        return None

    if method == "initialize":
        proto = params.get("protocolVersion") or "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "media-context", "version": __version__},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        try:
            data = _handle_tool_call(tool_name, tool_args)
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve_stdio() -> int:
    """Run MCP server over newline-delimited JSON-RPC on stdin/stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            err = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_rpc_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
