"""Налаштування бота з файлу .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} має бути цілим числом секунд.") from exc
    if value < 30:
        raise RuntimeError(f"{name} має бути не менше 30 секунд, щоб не перевантажувати OLX.")
    return value


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Вкажіть BOT_TOKEN у файлі .env. Зразок є в .env.example.")

CHECK_INTERVAL_SECONDS = _positive_int("CHECK_INTERVAL_SECONDS", 300)

_db_raw = os.getenv("DB_PATH", "olx_monitor.db").strip() or "olx_monitor.db"
_db_path = Path(_db_raw)
DB_PATH = _db_path if _db_path.is_absolute() else BASE_DIR / _db_path
