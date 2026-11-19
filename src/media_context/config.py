"""Configuration and model fallback chain management for media-context."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

SUPPORTED_BACKENDS = ("agy", "claude", "codex", "opencode")

DEFAULT_MODELS: Dict[str, str] = {
    "agy": "",
    "claude": "sonnet",
    "codex": "",
    "opencode": "",
}

DEFAULT_COOLDOWN_SECONDS = 300


@dataclass(frozen=True)
class FallbackTarget:
    backend: str
    model: str = ""

    @property
    def key(self) -> str:
        return f"{self.backend}:{self.model}" if self.model else self.backend

    def to_dict(self) -> Dict[str, str]:
        return {"backend": self.backend, "model": self.model}


def get_config_path() -> Path:
    custom = os.environ.get("MCTX_CONFIG", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home() / ".config" / "media-context" / "config.json"


def detect_available_backends() -> List[str]:
    return [b for b in SUPPORTED_BACKENDS if shutil.which(b)]


def parse_chain_string(spec: str) -> List[FallbackTarget]:
    """Parse comma-separated targets like 'agy:gemini-4-neon-medium,claude:sonnet,codex'."""
    targets: List[FallbackTarget] = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        if ":" in item:
            b, m = item.split(":", 1)
            targets.append(FallbackTarget(b.strip().lower(), m.strip()))
        else:
            targets.append(FallbackTarget(item.lower(), ""))
    return targets


def load_config() -> Dict[str, Any]:
    path = get_config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def build_default_chain() -> List[FallbackTarget]:
    avail = detect_available_backends()
    return [FallbackTarget(b, DEFAULT_MODELS.get(b, "")) for b in avail]


def resolve_fallback_chain(
    preferred_backend: Optional[str] = None,
    preferred_model: Optional[str] = None,
) -> List[FallbackTarget]:
    """Build ordered fallback chain: explicit args > env > config.json > auto-detected CLIs."""
    chain: List[FallbackTarget] = []

    # 1. Explicit primary target from CLI flag / MCP arg
    if preferred_backend and preferred_backend.lower() != "auto":
        chain.append(FallbackTarget(preferred_backend.lower(), (preferred_model or "").strip()))

    # 2. Environment variable chain
    env_chain = os.environ.get("MCTX_FALLBACK_CHAIN", "").strip()
    if env_chain:
        chain.extend(parse_chain_string(env_chain))
    else:
        # 3. Config file chain
        cfg = load_config()
        cfg_chain = cfg.get("fallback_chain")
        if isinstance(cfg_chain, list) and cfg_chain:
            for entry in cfg_chain:
                if isinstance(entry, dict) and entry.get("backend"):
                    chain.append(
                        FallbackTarget(
                            str(entry["backend"]).strip().lower(),
                            str(entry.get("model") or "").strip(),
                        )
                    )
                elif isinstance(entry, str) and entry.strip():
                    chain.extend(parse_chain_string(entry))

    # 4. Env single backend/model override if chain still empty
    if not chain:
        env_b = os.environ.get("MCTX_BACKEND", "").strip().lower()
        env_m = os.environ.get("MCTX_MODEL", "").strip()
        if env_b and env_b != "auto":
            chain.append(FallbackTarget(env_b, env_m))

    # 5. Append auto-detected available backends as safety net
    for t in build_default_chain():
        chain.append(t)

    # Deduplicate preserving order and validate backend names
    seen = set()
    result: List[FallbackTarget] = []
    for t in chain:
        if t.backend not in SUPPORTED_BACKENDS:
            continue
        if t.key in seen:
            continue
        seen.add(t.key)
        result.append(t)

    # If preferred_model was given with backend=auto, apply it to the first target
    if preferred_model and (not preferred_backend or preferred_backend.lower() == "auto") and result:
        first = FallbackTarget(result[0].backend, preferred_model.strip())
        result = [first] + [t for t in result if t.key != first.key]

    return result


def get_cooldown_seconds() -> int:
    env_val = os.environ.get("MCTX_COOLDOWN_SECONDS", "").strip()
    if env_val.isdigit():
        return int(env_val)
    cfg = load_config()
    val = cfg.get("cooldown_seconds")
    if isinstance(val, int) and val >= 0:
        return val
    return DEFAULT_COOLDOWN_SECONDS


def init_default_config(force: bool = False) -> Path:
    path = get_config_path()
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    chain = build_default_chain() or [
        FallbackTarget("agy", ""),
        FallbackTarget("claude", "sonnet"),
        FallbackTarget("codex", ""),
    ]
    payload = {
        "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
        "fallback_chain": [t.to_dict() for t in chain],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
