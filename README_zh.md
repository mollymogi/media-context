# media-context (`mctx`)

**面向图片、视频、音频的 `context-mode` + `rtk`。**

把重型多媒体文件挡在 Coding Agent 主会话上下文之外——**本地优先、0 额外 API 费用，全 CLI 通用（Antigravity / Claude Code / Codex / OpenCode / `dsh`）**。

[English README](./README.md)

---

## 为什么做这个工具？

`rtk` 和 `context-mode` 解决了**大段文本/日志**撑爆上下文的问题；但当你用 Agent 跑视频制作、抽帧质检、UI 截图对比、音频验收时，**图片和音频会静默把主会话拖垮**：

- 主会话一旦通过 `read_file` / `Read` 读入几十张抽帧图或几段 WAV/MP3，这些多模态块就会永久钉在历史记录里；
- 实测一次读入 30 张图 + 10 段音频后，上下文直接飙到 **~41 万 tokens**，哪怕后续只跑一条 1 秒钟的 `ls` 命令，每次模型首包等待（TTFT）都被拖到 **~5 分钟**。

---

## 核心架构（三级漏斗，0 额外 API 账单）

1. **L1 · 纯本地免费预处理（0 token、毫秒级）**
   - `mctx probe <file> --audio --black`：`ffprobe` 元数据 + 音频静音/爆音检测 + 视频黑屏检测，返回极简 JSON。
   - `mctx sheet <video_or_dir> --grid 3x3`：把整段视频或几十张抽帧图拼成 **1 张带时间戳水印的宫格缩略图**（减少 85%+ 视觉 token）。
   - `mctx shrink <image>`：把超大原图压缩为 ≤1024px 的轻量 JPG。
   - `mctx transcribe <audio>`：自动探测本机离线转写引擎（`mlx_whisper` / `whisper-cli` / `whisper`），0 成本把音频转纯文本。
2. **L2 · 复用已有订阅 CLI 的无状态隔离 Worker + SQLite 缓存**
   - `mctx inspect <files> -q "检查画面是否穿帮"`：自动把视频/多图压成宫格图，再拉起一个**一次性无历史上下文的订阅 CLI 子进程**（自动支持 `agy` / `claude -p` / `codex exec` / `opencode run`），只把简短文字结论吐回主会话。
   - **0 额外 API 费用**（走你已有订阅额度），**主会话 0 媒体残留**，并按文件 `sha256 + 问题` 缓存在本地 SQLite。
3. **L3 · 跨客户端 `PreToolUse` 硬防线 Hook**
   - `mctx hook run --client antigravity|claude|opencode --mode block|shrink`：拦截主会话手滑直读原图/音视频，内部 Worker 自动携带 `MCTX_BYPASS=1` 避免递归。

---

## 快速开始

```bash
git clone https://github.com/mollymogi/media-context.git
cd media-context
pip install -e .
mctx doctor
```

常用命令：

```bash
mctx probe video.mp4 --audio --black
mctx sheet video.mp4 --grid 3x3 --width 1280
mctx inspect video.mp4 -q "检查人物手部是否穿帮、画面有无闪烁"
mctx stats
```

MCP 通用配置（Antigravity / Claude Code / Codex / OpenCode）：

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

生成 Hook 配置：

```bash
mctx hook config antigravity --mode block
mctx hook config claude --mode block
```
