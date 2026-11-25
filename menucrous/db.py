"""Stockage SQLite (/data/menucrous.db) : réglages par serveur Discord et état des notes."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS menu_config (
    guild_id     INTEGER PRIMARY KEY,
    channel_id   INTEGER NOT NULL,
    heure        TEXT    NOT NULL DEFAULT '08:00',   -- HH:MM, heure de Paris
    restaurants  TEXT    NOT NULL DEFAULT 'manufacture,puvis',
    complet      INTEGER NOT NULL DEFAULT 0,
    actif        INTEGER NOT NULL DEFAULT 1,
    dernier_envoi TEXT                               -- date ISO du dernier envoi auto
);
CREATE TABLE IF NOT EXISTS notes_config (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    actif      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS kv (
    cle    TEXT PRIMARY KEY,
    valeur TEXT NOT NULL
);
"""


@dataclass
class MenuConfig:
    guild_id: int
    channel_id: int
    heure: str
    restaurants: list[str]
    complet: bool
    actif: bool
    dernier_envoi: str | None


@dataclass
class NotesConfig:
    guild_id: int
    channel_id: int
    actif: bool


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ── Menu ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _menu(row) -> MenuConfig:
        return MenuConfig(
            row["guild_id"], row["channel_id"], row["heure"],
            [r for r in row["restaurants"].split(",") if r], bool(row["complet"]),
            bool(row["actif"]), row["dernier_envoi"],
        )

    async def menu_configs(self) -> list[MenuConfig]:
        async with self.conn.execute("SELECT * FROM menu_config") as cur:
            return [self._menu(r) for r in await cur.fetchall()]

    async def menu_config(self, guild_id: int) -> MenuConfig | None:
        async with self.conn.execute("SELECT * FROM menu_config WHERE guild_id=?", (guild_id,)) as cur:
            row = await cur.fetchone()
        return self._menu(row) if row else None

    async def set_menu_config(self, c: MenuConfig) -> None:
        await self.conn.execute(
            """INSERT INTO menu_config (guild_id, channel_id, heure, restaurants, complet, actif, dernier_envoi)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, heure=excluded.heure,
                 restaurants=excluded.restaurants, complet=excluded.complet, actif=excluded.actif,
                 dernier_envoi=excluded.dernier_envoi""",
            (c.guild_id, c.channel_id, c.heure, ",".join(c.restaurants), int(c.complet),
             int(c.actif), c.dernier_envoi),
        )
        await self.conn.commit()

    async def mark_menu_sent(self, guild_id: int, jour: str) -> None:
        await self.conn.execute("UPDATE menu_config SET dernier_envoi=? WHERE guild_id=?", (jour, guild_id))
        await self.conn.commit()

    # ── Notes ─────────────────────────────────────────────────────────────────
    async def notes_configs(self) -> list[NotesConfig]:
        async with self.conn.execute("SELECT * FROM notes_config") as cur:
            return [NotesConfig(r["guild_id"], r["channel_id"], bool(r["actif"])) for r in await cur.fetchall()]

    async def notes_config(self, guild_id: int) -> NotesConfig | None:
        async with self.conn.execute("SELECT * FROM notes_config WHERE guild_id=?", (guild_id,)) as cur:
            r = await cur.fetchone()
        return NotesConfig(r["guild_id"], r["channel_id"], bool(r["actif"])) if r else None

    async def set_notes_config(self, c: NotesConfig) -> None:
        await self.conn.execute(
            """INSERT INTO notes_config (guild_id, channel_id, actif) VALUES (?,?,?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, actif=excluded.actif""",
            (c.guild_id, c.channel_id, int(c.actif)),
        )
        await self.conn.commit()

    # ── Clé / valeur (instantané des notes, etc.) ─────────────────────────────
    async def get_json(self, cle: str, defaut=None):
        async with self.conn.execute("SELECT valeur FROM kv WHERE cle=?", (cle,)) as cur:
            r = await cur.fetchone()
        return json.loads(r["valeur"]) if r else defaut

    async def set_json(self, cle: str, valeur) -> None:
        await self.conn.execute(
            "INSERT INTO kv (cle, valeur) VALUES (?,?) ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
            (cle, json.dumps(valeur, ensure_ascii=False)),
        )
        await self.conn.commit()
