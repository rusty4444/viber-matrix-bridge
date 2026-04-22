"""SQLite state for the Viber bridge.

Tables:
  mappings(viber_name PK, matrix_room_id, created_at)
  seen_messages(hash PK, direction, ts)       -- for dedup / echo suppression
  meta(key PK, value)
"""

from __future__ import annotations
import asyncio
import hashlib
import time
from pathlib import Path
from typing import Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS mappings (
    viber_name TEXT PRIMARY KEY,
    matrix_room_id TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_messages (
    hash TEXT PRIMARY KEY,
    direction TEXT NOT NULL,        -- 'viber->matrix' or 'matrix->viber'
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen_messages(ts);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def hash_msg(viber_name: str, sender: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(viber_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(sender.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.strip().encode("utf-8"))
    return h.hexdigest()


class State:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # Mappings ---------------------------------------------------------
    async def get_room_for_viber(self, viber_name: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT matrix_room_id FROM mappings WHERE viber_name=?",
                (viber_name,),
            )
            row = await cur.fetchone()
            return row[0] if row else None

    async def get_viber_for_room(self, room_id: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT viber_name FROM mappings WHERE matrix_room_id=?",
                (room_id,),
            )
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_mapping(self, viber_name: str, room_id: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO mappings(viber_name, matrix_room_id, created_at) VALUES (?,?,?)",
                (viber_name, room_id, int(time.time())),
            )
            await db.commit()

    async def delete_mapping(self, viber_name: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM mappings WHERE viber_name=?", (viber_name,)
            )
            await db.commit()

    async def list_mappings(self) -> list[tuple[str, str]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT viber_name, matrix_room_id FROM mappings ORDER BY viber_name"
            )
            return [tuple(r) for r in await cur.fetchall()]

    # Dedup ------------------------------------------------------------
    async def seen(self, msg_hash: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM seen_messages WHERE hash=?", (msg_hash,)
            )
            return (await cur.fetchone()) is not None

    async def mark_seen(self, msg_hash: str, direction: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO seen_messages(hash, direction, ts) VALUES (?,?,?)",
                (msg_hash, direction, int(time.time())),
            )
            await db.commit()

    async def purge_old(self, older_than_seconds: int):
        cutoff = int(time.time()) - older_than_seconds
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM seen_messages WHERE ts<?", (cutoff,))
            await db.commit()
