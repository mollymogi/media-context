# media-context (`mctx`)

**`context-mode` + `rtk` for Images, Video, and Audio.**

Keep heavy media out of your coding agent's context window — **local-first, zero extra API cost, works across Antigravity (`agy`), Claude Code, Codex, OpenCode, `dsh`, and any shell-capable agent.**

[中文文档](./README_zh.md)

---

## Why?

Tools like `rtk` and `context-mode` keep **large text logs** out of your agent's context.
But if you work on video pipelines, UI screenshots, design QA, or audio workflows, **images and audio silently destroy your context window**:

| Real-World Session | Raw `Read` / `read_file` | With `media-context` (`mctx`) |
| :--- | :--- | :--- |
| 30 images + 10 audio clips in one session | **~410k tokens** (60%+ media bloat) | **~30k tokens** (text findings only) |
| Time-to-First-Token (TTFT) per tool step | **285–324s (~5 min/step)** | **3–8s** |
| Extra Vision API billing | N/A or extra API keys | **$0 extra** (local FFmpeg + your existing CLI subscription) |

Every time your agent reads a raw PNG, frame extract, or WAV file directly into the main conversation, that binary payload stays pinned in history for every subsequent turn.

---

## How It Works (3-Stage Funnel, Zero Extra API Cost)

```
Raw Video / Images / Audio
   │
   ├─► L1: Local Zero-Token Tools (FFmpeg / FFprobe / Local Whisper)
   │       • mctx probe      → compact JSON metadata + silence/clipping/black-frame checks
   │       • mctx sheet      → stitch video or N frames into 1 timestamped contact sheet JPEG
   │       • mctx shrink     → downscale huge 4K PNGs into lightweight JPEGs
   │       • mctx transcribe → offline local Whisper transcription (mlx-whisper / whisper-cpp)
   │
   ├─► L2: Stateless Subscription CLI Worker + SQLite Cache (0 extra API bill)
   │       • mctx inspect    → spawns a disposable, stateless worker using your existing CLI
   │                           (agy / claude -p / codex exec / opencode run), inspects the
   │                           compressed contact sheet, caches by sha256 in SQLite, and
   │                           returns ONLY concise text to your main session.
   │
   └─► L3: Cross-Client PreToolUse Guard Hook
           • mctx hook run   → blocks accidental raw media Read/read_file (or auto-shrinks),
                               with automatic MCTX_BYPASS=1 for internal workers.
```

- **Zero Python third-party dependencies** (Python 3.9+ stdlib + `ffmpeg`/`ffprobe`).
- **One core, three interfaces**: CLI (`mctx`), stdio MCP server (`mctx mcp`), and `PreToolUse` hook (`mctx hook`).

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/mollymogi/media-context.git
cd media-context
pip install -e .
# Or with pipx / uv:
# pipx install .
```

Verify your local toolchain and detected subscription CLIs:

```bash
mctx doctor
```

### 2. CLI Usage (works in `dsh`, scripts, and every agent)

```bash
# Zero-token metadata + audio volume/silence + video black-frame check
mctx probe clip.mp4 --audio --black

# Compress a video or folder of 30 frames into 1 timestamped 3x3 contact sheet
mctx sheet clip.mp4 --grid 3x3 --width 1280
mctx sheet ./frames/ --grid 3x3

# Shrink a single large screenshot
mctx shrink hero_4k.png --max-side 1024

# Stateless visual QA using your existing CLI subscription (zero main-session leak, cached in SQLite)
mctx inspect clip.mp4 -q "Check if the character's hands deform or if any frame is corrupted."
mctx inspect ./frames/ -q "Compare lighting consistency across frames" --backend claude

# Offline local audio transcription (auto-detects mlx_whisper / whisper-cli / whisper)
mctx transcribe voiceover.wav -l zh

# Check cumulative bytes & tokens saved
mctx stats
```

### 3. Connect via MCP (Antigravity, Claude Code, Codex, OpenCode)

Add `mctx mcp` to your client's MCP configuration (only 6 compact tools exposed):

```json
{
  "mcpServers": {
    "media-context": {
      "command": "mctx",
      "args": ["mcp"]
    }
  }
}
```

### 4. Enable `PreToolUse` Guard Hook (Optional Hard Guardrail)

Generate ready-to-paste hook snippets for your client:

```bash
mctx hook config antigravity --mode block
mctx hook config claude --mode block
mctx hook config claude --mode shrink
```

- `--mode block`: blocks raw image/audio/video `Read` / `read_file` and guides the agent to `mctx probe / sheet / inspect` (whitelisted `mctx_sheet_*` and `mctx_shrink_*` artifacts pass through).
- `--mode shrink`: blocks raw audio/video, and transparently downscales oversized images before reading.

---

## Default Models & Automatic Quota/Capacity Fallback

`mctx inspect` supports an ordered **fallback chain** with automatic cooldown so your workflow never stalls when a model hits quota (`429`), capacity limits (`503 No capacity`), or auth expiry (`401`):

```bash
# Generate ~/.config/media-context/config.json
mctx config init
mctx config show
```

Example `~/.config/media-context/config.json`:

```json
{
  "cooldown_seconds": 300,
  "fallback_chain": [
    {"backend": "agy", "model": "gemini-4-neon-medium"},
    {"backend": "claude", "model": "sonnet"},
    {"backend": "codex", "model": ""},
    {"backend": "opencode", "model": ""}
  ]
}
```

When the primary model hits quota or `503 No capacity`, `mctx`:
1. Marks that `(backend, model)` in a local SQLite cooldown window (default 5 min),
2. Automatically switches to the next available target in the chain,
3. Emits a clear fallback notice (`[mctx:fallback] ...`) in both CLI and MCP output.

Run `mctx config reset-cooldown` to clear cooldowns immediately.

## Environment Variables

- `MCTX_FALLBACK_CHAIN`: Comma-separated override (e.g. `agy:gemini-4-neon-medium,claude:sonnet,codex`).
- `MCTX_CONFIG`: Custom config JSON path (default: `~/.config/media-context/config.json`).
- `MCTX_COOLDOWN_SECONDS`: Cooldown duration after quota/capacity errors (default: `300`).
- `MCTX_BACKEND` / `MCTX_MODEL`: Single backend/model override.
- `MCTX_HOME`: Cache & SQLite directory (default: `~/.cache/media-context`).
- `MCTX_BYPASS=1`: Bypass the `PreToolUse` hook (automatically set inside `mctx inspect` workers).

## License

MIT
