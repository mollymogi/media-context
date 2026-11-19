---
name: media-context
description: Keep images, video, and audio out of coding agent context windows using local-first mctx (media-context) tools. Use whenever inspecting, auditing, probing, or reviewing images, video frames, screenshots, or audio files to avoid context bloat and slow TTFT, with zero extra API cost.
---

# media-context (`mctx`) Skill

Use `mctx` (CLI or MCP tools `media_probe`, `media_sheet`, `media_shrink`, `media_transcribe`, `media_inspect`) instead of directly reading raw images, audio clips, or video frames into the main conversation context.

## Core Rules

1. **Never bulk-read raw media into the main session.** Reading dozens of screenshots, extracted frames, or audio files pins megabytes of multimodal tokens in conversation history and degrades TTFT for every subsequent turn.
2. **Always follow the 3-stage funnel (cheapest first):**
   - **Stage 1 — Zero-token local checks (`mctx probe` / `media_probe`)**: Check duration, resolution, fps, codecs, audio volume/silence (`--audio`), and video black frames (`--black`) locally in < 0.2s.
   - **Stage 2 — Local compression & offline transcription**:
     - For videos or multiple extracted frames: run `mctx sheet <video_or_dir> --grid 3x3 --width 1280` (`media_sheet`) to stitch frames into a single timestamped contact sheet JPEG.
     - For audio speech/dialogue QA: run `mctx transcribe <audio>` (`media_transcribe`) using the local free Whisper engine.
     - For a single oversized image: run `mctx shrink <image> --max-side 1024` (`media_shrink`).
   - **Stage 3 — Context-isolated visual/audio QA (`mctx inspect` / `media_inspect`)**: Delegate visual/audio inspection to a disposable, stateless subscription CLI worker (`agy` / `claude` / `codex` / `opencode`) with automatic quota/capacity fallback and SQLite caching. Only concise text findings enter the main conversation.
3. **Prefer zero extra API cost.** Do not invoke paid per-token vision APIs when `mctx` local tools or existing subscription CLI backends can answer the question.

## Quick Command Reference

```bash
mctx probe clip.mp4 --audio --black
mctx sheet clip.mp4 --grid 3x3 --width 1280
mctx sheet ./frames/ --grid 3x3
mctx shrink screenshot_4k.png --max-side 1024
mctx transcribe voice.wav -l zh
mctx inspect clip.mp4 -q "Check for visual artifacts, hand deformation, or flickering."
mctx config show
mctx stats
```
