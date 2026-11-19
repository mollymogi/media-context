"""Command-line interface for media-context (mctx)."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from typing import List, Optional

from . import __version__
from .cache import CacheStore, get_data_dir
from .hook import render_hook_config, run_hook_stdio
from .inspector import SUPPORTED_BACKENDS, detect_available_backends, inspect_media
from .mcp_server import serve_stdio
from .media_ops import make_contact_sheet, shrink_image
from .probe import probe_media
from .transcribe import detect_transcribe_engine, transcribe_audio


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_doctor(_args: argparse.Namespace) -> int:
    info = {
        "version": __version__,
        "data_dir": str(get_data_dir()),
        "ffmpeg": shutil.which("ffmpeg") or "MISSING",
        "ffprobe": shutil.which("ffprobe") or "MISSING",
        "transcribe_engine": detect_transcribe_engine() or "NONE (optional)",
        "subscription_backends": detect_available_backends(),
        "supported_backends": list(SUPPORTED_BACKENDS),
    }
    _print_json(info)
    return 0 if info["ffmpeg"] != "MISSING" and info["ffprobe"] != "MISSING" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mctx",
        description="media-context: Keep images, video, and audio out of your coding agent's context window.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"mctx {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # probe
    p_probe = sub.add_parser("probe", help="Zero-token ffprobe metadata + optional audio/black checks")
    p_probe.add_argument("path", help="Path to image, audio, or video file")
    p_probe.add_argument("--audio", action="store_true", help="Detect volume levels & silence")
    p_probe.add_argument("--black", action="store_true", help="Detect black frames in video")

    # sheet
    p_sheet = sub.add_parser("sheet", help="Create 1 timestamped contact sheet from a video or image list/dir")
    p_sheet.add_argument("inputs", nargs="+", help="1 video file or multiple image paths / directories")
    p_sheet.add_argument("--grid", default="3x3", help="Grid layout, e.g. 3x3, 4x3 (default: 3x3)")
    p_sheet.add_argument("--width", type=int, default=1280, help="Total width in pixels (default: 1280)")
    p_sheet.add_argument("--quality", type=int, default=78, help="JPEG quality 1-100 (default: 78)")
    p_sheet.add_argument("--no-timestamps", action="store_true", help="Disable timestamp overlay")
    p_sheet.add_argument("-o", "--output", help="Optional output file path")

    # shrink
    p_shrink = sub.add_parser("shrink", help="Downscale and compress a single large image")
    p_shrink.add_argument("path", help="Path to image")
    p_shrink.add_argument("--max-side", type=int, default=1024, help="Max width/height px (default: 1024)")
    p_shrink.add_argument("--quality", type=int, default=78, help="JPEG quality 1-100 (default: 78)")
    p_shrink.add_argument("-o", "--output", help="Optional output file path")

    # transcribe
    p_tr = sub.add_parser("transcribe", help="Zero-cost local offline audio/video transcription")
    p_tr.add_argument("path", help="Path to audio or video file")
    p_tr.add_argument("-l", "--language", help="Language code (e.g. zh, en)")
    p_tr.add_argument("-m", "--model", help="Optional local whisper model name/path")

    # inspect
    p_ins = sub.add_parser("inspect", help="Stateless inspection via existing subscription CLI (0 extra API cost)")
    p_ins.add_argument("inputs", nargs="+", help="Media files or directories to inspect")
    p_ins.add_argument("-q", "--question", required=True, help="Inspection question / checklist")
    p_ins.add_argument("--backend", default=None, help="Backend CLI: auto|agy|claude|codex|opencode")
    p_ins.add_argument("--model", default=None, help="Optional model override for backend CLI")
    p_ins.add_argument("--grid", default="3x3", help="Contact sheet grid for video/multi-images")
    p_ins.add_argument("--no-cache", action="store_true", help="Bypass SQLite cache")
    p_ins.add_argument("--json", action="store_true", dest="as_json", help="Print full JSON metadata")

    # mcp
    sub.add_parser("mcp", help="Start stdio JSON-RPC MCP server")

    # hook
    p_hook = sub.add_parser("hook", help="PreToolUse hook runner & config generator")
    hook_sub = p_hook.add_subparsers(dest="hook_cmd", required=True)
    p_hrun = hook_sub.add_parser("run", help="Read PreToolUse JSON from stdin and evaluate")
    p_hrun.add_argument("--client", default="generic", choices=["generic", "antigravity", "claude", "opencode"])
    p_hrun.add_argument("--mode", default="block", choices=["block", "shrink"])
    p_hrun.add_argument("--max-side", type=int, default=1024)
    p_hcfg = hook_sub.add_parser("config", help="Print ready-to-paste hook JSON snippet")
    p_hcfg.add_argument("client", choices=["antigravity", "claude", "opencode"])
    p_hcfg.add_argument("--mode", default="block", choices=["block", "shrink"])

    # stats
    sub.add_parser("stats", help="Show cumulative bytes and estimated tokens saved")

    # doctor
    sub.add_parser("doctor", help="Check local dependencies and available subscription CLI backends")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = CacheStore()

    try:
        if args.command == "probe":
            _print_json(probe_media(args.path, check_audio=args.audio, check_black=args.black, store=store))
            return 0
        if args.command == "sheet":
            _print_json(
                make_contact_sheet(
                    args.inputs,
                    grid=args.grid,
                    width=args.width,
                    quality=args.quality,
                    timestamps=not args.no_timestamps,
                    output=args.output,
                    store=store,
                )
            )
            return 0
        if args.command == "shrink":
            _print_json(
                shrink_image(
                    args.path,
                    max_side=args.max_side,
                    quality=args.quality,
                    output=args.output,
                    store=store,
                )
            )
            return 0
        if args.command == "transcribe":
            _print_json(transcribe_audio(args.path, language=args.language, model=args.model, store=store))
            return 0
        if args.command == "inspect":
            res = inspect_media(
                args.inputs,
                args.question,
                backend=args.backend,
                model=args.model,
                grid=args.grid,
                no_cache=args.no_cache,
                store=store,
            )
            if args.as_json:
                _print_json(res)
            else:
                tag = "cached" if res.get("cached") else f"{res['backend']} {res['elapsed_s']}s"
                print(f"[mctx:{tag}]\n{res['result']}")
            return 0
        if args.command == "mcp":
            return serve_stdio()
        if args.command == "hook":
            if args.hook_cmd == "run":
                return run_hook_stdio(client=args.client, mode=args.mode, max_side=args.max_side)
            if args.hook_cmd == "config":
                _print_json(render_hook_config(args.client, mode=args.mode))
                return 0
        if args.command == "stats":
            _print_json(store.get_stats())
            return 0
        if args.command == "doctor":
            return cmd_doctor(args)
    except Exception as exc:
        print(f"mctx error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
