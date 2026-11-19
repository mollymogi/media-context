"""SQLite cache and savings telemetry for media-context."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_data_dir() -> Path:
    base = Path(os.environ.get("MCTX_HOME", Path.home() / ".cache" / "media-context"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_db_path() -> Path:
    return get_data_dir() / "mctx.db"


def get_artifacts_dir() -> Path:
    d = get_data_dir() / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_files(paths: List[str | Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(str(Path(x).resolve()) for x in paths):
        h.update(p.encode("utf-8"))
        h.update(b":")
        h.update(sha256_file(p).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def estimate_media_tokens(path: str | Path) -> int:
    """Conservative token estimate for raw media ingested directly into an LLM context."""
    p = Path(path)
    if not p.exists():
        return 0
    size = p.stat().st_size
    ext = p.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}:
        # Typical vision tiling: ~1,500 - 4,000 tokens for HD/4K images
        return max(1200, min(6000, int(size / 250)))
    if ext in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}:
        # Audio payloads consume heavily when passed raw (~32 tokens/sec, rough size proxy)
        return max(1000, int(size / 120))
    if ext in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
        # Raw video frames / multimodal video chunks
        return max(5000, int(size / 400))
    return int(size / 4)


class CacheStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS inspect_cache (
                    cache_key TEXT PRIMARY KEY,
                    file_hash TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS savings_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    action TEXT NOT NULL,
                    source_bytes INTEGER NOT NULL DEFAULT 0,
                    output_bytes INTEGER NOT NULL DEFAULT 0,
                    saved_tokens_est INTEGER NOT NULL DEFAULT 0,
                    detail TEXT DEFAULT ''
                );
                """
            )

    def make_key(self, file_hash: str, backend: str, model: str, prompt: str) -> str:
        raw = json.dumps(
            {"h": file_hash, "b": backend, "m": model, "p": prompt.strip()},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_inspect(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result, backend, model, created_at, hits FROM inspect_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE inspect_cache SET hits = hits + 1 WHERE cache_key = ?",
                (cache_key,),
            )
            return {
                "result": row[0],
                "backend": row[1],
                "model": row[2],
                "created_at": row[3],
                "hits": row[4] + 1,
                "cached": True,
            }

    def put_inspect(
        self,
        cache_key: str,
        file_hash: str,
        backend: str,
        model: str,
        prompt: str,
        result: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO inspect_cache
                (cache_key, file_hash, backend, model, prompt, result, created_at, hits)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (cache_key, file_hash, backend, model, prompt.strip(), result, time.time()),
            )

    def record_saving(
        self,
        action: str,
        source_bytes: int,
        output_bytes: int,
        saved_tokens_est: int,
        detail: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO savings_log (ts, action, source_bytes, output_bytes, saved_tokens_est, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (time.time(), action, source_bytes, output_bytes, max(0, saved_tokens_est), detail[:200]),
            )

    def get_stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(source_bytes), 0),
                       COALESCE(SUM(output_bytes), 0),
                       COALESCE(SUM(saved_tokens_est), 0)
                FROM savings_log
                """
            ).fetchone()
            by_action = conn.execute(
                """
                SELECT action, COUNT(*), COALESCE(SUM(saved_tokens_est), 0)
                FROM savings_log
                GROUP BY action
                ORDER BY SUM(saved_tokens_est) DESC
                """
            ).fetchall()
            cache_row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM inspect_cache"
            ).fetchone()

        return {
            "total_events": row[0],
            "source_bytes": row[1],
            "output_bytes": row[2],
            "saved_bytes": max(0, row[1] - row[2]),
            "saved_tokens_est": row[3],
            "cache_entries": cache_row[0],
            "cache_hits": cache_row[1],
            "by_action": [
                {"action": a, "count": c, "saved_tokens_est": t} for a, c, t in by_action
            ],
        }
