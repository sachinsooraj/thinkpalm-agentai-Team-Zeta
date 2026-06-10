"""
sentinel/utils.py — Shared helpers for TPOps Sentinel
"""

from __future__ import annotations

import os
import logging
import yaml
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

# ── Load .env if it exists ────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=False)


# ── Logging setup ─────────────────────────────────────────────────────────────
def get_logger(name: str) -> logging.Logger:
    """Return a consistently-formatted logger."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        fmt = logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


# ── Config loader ─────────────────────────────────────────────────────────────
_config_cache: dict | None = None


def load_config(path: Path | None = None) -> dict:
    """Load and cache config.yaml, merging environment overrides."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    cfg_path = path or (_ROOT / "config.yaml")
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    # Environment overrides
    dry_run_env = os.getenv("SENTINEL_DRY_RUN", "").lower()
    if dry_run_env in ("true", "1", "yes"):
        cfg["monitor"]["dry_run"] = True

    github_repo = os.getenv("GITHUB_REPO")
    if github_repo:
        cfg["alerting"]["github"]["repo"] = github_repo

    _config_cache = cfg
    return cfg


def is_dry_run() -> bool:
    """Return True if running in dry-run mode (no real API calls)."""
    cfg = load_config()
    return cfg["monitor"].get("dry_run", False)


# ── Time helpers ──────────────────────────────────────────────────────────────
def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def format_duration_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms/1000:.2f} s"


# ── Severity helpers ──────────────────────────────────────────────────────────
def severity_emoji(level: str) -> str:
    return {"critical": "🔴", "warning": "🟡", "info": "🟢"}.get(level, "⚪")


def severity_color(level: str) -> str:
    return {"critical": "#FF4B4B", "warning": "#FFA500", "info": "#21C55D"}.get(
        level, "#808080"
    )


def reset_config_cache() -> None:
    """Reset config cache (used in tests)."""
    global _config_cache
    _config_cache = None
