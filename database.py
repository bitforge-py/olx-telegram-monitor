"""Збереження відстежень і історії оголошень у SQLite."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from parser import Ad

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    monitoring_enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    seeded INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, url)
);

CREATE TABLE IF NOT EXISTS seen_ads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    watch_id INTEGER NOT NULL,
    ad_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    price TEXT,
    location_date TEXT,
    photo_url TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, ad_id),
    FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_seen_user_url ON seen_ads (user_id, url);
CREATE INDEX IF NOT EXISTS idx_seen_watch ON seen_ads (watch_id);
CREATE INDEX IF NOT EXISTS idx_watches_user ON watches (user_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class Watch:
    id: int
    user_id: int
    url: str
    seeded: bool
    seen_count: int
    last_error: str | None
    created_at: str


def _watch_from_row(row: aiosqlite.Row) -> Watch:
    return Watch(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        url=str(row["url"]),
        seeded=bool(row["seeded"]),
        seen_count=int(row["seen_count"]),
        last_error=row["last_error"],
        created_at=str(row["created_at"]),
    )


_WATCH_COLUMNS = """
    w.id,
    w.user_id,
    w.url,
    w.seeded,
    w.last_error,
    w.created_at,
    (SELECT COUNT(*) FROM seen_ads s WHERE s.watch_id = w.id) AS seen_count
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База даних не відкрита.")
        return self._conn

    async def ensure_user(self, user_id: int) -> None:
        async with self._lock:
            conn = self._connection()
            await conn.execute(
                """
                INSERT INTO user_settings (user_id, monitoring_enabled)
                VALUES (?, 1)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (user_id,),
            )
            await conn.commit()

    async def monitoring_enabled(self, user_id: int) -> bool:
        async with self._lock:
            conn = self._connection()
            cursor = await conn.execute(
                "SELECT monitoring_enabled FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return True
            return bool(row["monitoring_enabled"])

    async def set_monitoring(self, user_id: int, enabled: bool) -> None:
        async with self._lock:
            conn = self._connection()
            await conn.execute(
                """
                INSERT INTO user_settings (user_id, monitoring_enabled)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    monitoring_enabled = excluded.monitoring_enabled
                """,
                (user_id, 1 if enabled else 0),
            )
            await conn.commit()

    async def has_watch(self, user_id: int, url: str) -> bool:
        async with self._lock:
            conn = self._connection()
            cursor = await conn.execute(
                "SELECT 1 FROM watches WHERE user_id = ? AND url = ? LIMIT 1",
                (user_id, url),
            )
            return await cursor.fetchone() is not None

    async def count_watches(self, user_id: int) -> int:
        async with self._lock:
            conn = self._connection()
            cursor = await conn.execute(
                "SELECT COUNT(*) AS total FROM watches WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return int(row["total"]) if row else 0

    async def add_watch(
        self,
        user_id: int,
        url: str,
        ads: list[Ad] | None,
    ) -> Watch | None:
        """Додає посилання. None у ads означає, що список ще не вдалося прочитати."""
        async with self._lock:
            conn = self._connection()
            existing = await conn.execute(
                "SELECT id FROM watches WHERE user_id = ? AND url = ?",
                (user_id, url),
            )
            if await existing.fetchone():
                return None

            now = _now()
            seeded = ads is not None
            cursor = await conn.execute(
                """
                INSERT INTO watches (user_id, url, seeded, last_checked_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, url, 1 if seeded else 0, now if seeded else None, now),
            )
            watch_id = int(cursor.lastrowid or 0)
            if watch_id <= 0:
                raise RuntimeError("Не вдалося зберегти посилання.")
            if ads:
                await _insert_ads(conn, user_id, watch_id, ads, now)
            await conn.commit()
            return Watch(
                id=watch_id,
                user_id=user_id,
                url=url,
                seeded=seeded,
                seen_count=len(ads or []),
                last_error=None,
                created_at=now,
            )

    async def list_watches(self, user_id: int) -> list[Watch]:
        async with self._lock:
            conn = self._connection()
            cursor = await conn.execute(
                f"""
                SELECT {_WATCH_COLUMNS}
                FROM watches w
                WHERE w.user_id = ?
                ORDER BY w.id
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [_watch_from_row(row) for row in rows]

    async def list_active_watches(self) -> list[Watch]:
        async with self._lock:
            conn = self._connection()
            cursor = await conn.execute(
                f"""
                SELECT {_WATCH_COLUMNS}
                FROM watches w
                JOIN user_settings u ON u.user_id = w.user_id
                WHERE u.monitoring_enabled = 1
                ORDER BY w.id
                """
            )
            rows = await cursor.fetchall()
            return [_watch_from_row(row) for row in rows]

    async def delete_watch(self, user_id: int, watch_id: int) -> bool:
        async with self._lock:
            conn = self._connection()
            cursor = await conn.execute(
                "DELETE FROM watches WHERE id = ? AND user_id = ?",
                (watch_id, user_id),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def is_seen(self, user_id: int, ad_id: str, url: str) -> bool:
        async with self._lock:
            conn = self._connection()
            cursor = await conn.execute(
                """
                SELECT 1
                FROM seen_ads
                WHERE user_id = ? AND (ad_id = ? OR url = ?)
                LIMIT 1
                """,
                (user_id, ad_id, url),
            )
            return await cursor.fetchone() is not None

    async def remember_ads(self, watch: Watch, ads: list[Ad]) -> None:
        if not ads:
            return
        async with self._lock:
            conn = self._connection()
            await _insert_ads(conn, watch.user_id, watch.id, ads, _now())
            await conn.commit()

    async def mark_seeded(self, watch_id: int) -> None:
        async with self._lock:
            conn = self._connection()
            await conn.execute(
                """
                UPDATE watches
                SET seeded = 1, last_error = NULL, last_checked_at = ?
                WHERE id = ?
                """,
                (_now(), watch_id),
            )
            await conn.commit()

    async def mark_checked(self, watch_id: int) -> None:
        async with self._lock:
            conn = self._connection()
            await conn.execute(
                """
                UPDATE watches
                SET last_error = NULL, last_checked_at = ?
                WHERE id = ?
                """,
                (_now(), watch_id),
            )
            await conn.commit()

    async def set_watch_error(self, watch_id: int, message: str) -> None:
        async with self._lock:
            conn = self._connection()
            await conn.execute(
                """
                UPDATE watches
                SET last_error = ?, last_checked_at = ?
                WHERE id = ?
                """,
                (message[:300], _now(), watch_id),
            )
            await conn.commit()


async def _insert_ads(
    conn: aiosqlite.Connection,
    user_id: int,
    watch_id: int,
    ads: list[Ad],
    created_at: str,
) -> None:
    await conn.executemany(
        """
        INSERT OR IGNORE INTO seen_ads (
            user_id, watch_id, ad_id, url, title, price, location_date, photo_url, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                user_id,
                watch_id,
                ad.ad_id,
                ad.url,
                ad.title,
                ad.price,
                ad.location_date,
                ad.photo_url,
                created_at,
            )
            for ad in ads
        ],
    )
