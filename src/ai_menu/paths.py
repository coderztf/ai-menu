"""Paths to runtime data and packaged static assets."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "db.sqlite3"
DEFAULT_AI_CONFIG = PROJECT_ROOT / "ai-config.yaml"
STATIC_ROOT = Path(__file__).with_name("h5")
