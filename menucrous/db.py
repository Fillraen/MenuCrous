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
-- Emploi du temps : plusieurs règles indépendantes par serveur (une par type), chacune
-- avec son salon et son heure.
CREATE TABLE IF NOT EXISTS edt_regle (
    guild_id     INTEGER NOT NULL,
    type         TEXT    NOT NULL,        -- semaine | jour | modifs
    channel_id   INTEGER NOT NULL,
    heure        TEXT    NOT NULL DEFAULT '07:30',
    jour_semaine INTEGER NOT NULL DEFAULT 0,  -- « semaine » : 0 = lundi … 6 = dimanche
    actif        INTEGER NOT NULL DEFAULT 1,
    dernier_envoi TEXT,                   -- date ISO du dernier envoi auto
    PRIMARY KEY (guild_id, type)
);
-- SoWeSign : un compte par utilisateur Discord. Code et signatures chiffrés (Fernet, SWS_SECRET).
CREATE TABLE IF NOT EXISTS sws_compte (
    user_id    INTEGER PRIMARY KEY,
    code       TEXT    NOT NULL,             -- chiffré : établissement + identifiant + PIN
    nom        TEXT    NOT NULL DEFAULT '',
    signatures TEXT,                         -- chiffré : liste JSON de data URL PNG
    notif      INTEGER NOT NULL DEFAULT 1,   -- MP quand une signature est à faire
    connus     TEXT    NOT NULL DEFAULT '[]',-- signatures en attente déjà notifiées
    rappel     TEXT,                         -- date ISO du dernier rappel quotidien
    cree       TEXT    NOT NULL
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
class EdtRegle:
    guild_id: int
    type: str  # semaine | jour | modifs
    channel_id: int
    heure: str = "07:30"
    jour_semaine: int = 0
    actif: bool = True
    dernier_envoi: str | None = None


@dataclass
class SwsCompte:
    user_id: int
    code: str  # en clair en mémoire uniquement
    nom: str
    signatures: list[str]
    notif: bool
    connus: list[str]
    rappel: str | None
    cree: str


@dataclass
class NotesConfig:
    guild_id: int
    channel_id: int
    actif: bool


class Database:
    def __init__(self, path: Path, secret: str = ""):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.fernet = None
        if secret:
            from cryptography.fernet import Fernet

            self.fernet = Fernet(secret.encode())

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self._migrer_edt()
        await self.conn.commit()

    async def _migrer_edt(self) -> None:
        """Ancienne table edt_config (une règle « mode » par serveur) → edt_regle."""
        async with self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='edt_config'") as cur:
            if not await cur.fetchone():
                return
        async with self.conn.execute("SELECT * FROM edt_config") as cur:
            anciens = await cur.fetchall()
        for r in anciens:
            types = {"semaine": ["semaine"], "jour": ["jour"], "les2": ["semaine", "jour"]}[r["mode"]]
            if r["modifs"]:
                types.append("modifs")
            for t in types:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO edt_regle (guild_id, type, channel_id, heure, actif) VALUES (?,?,?,?,?)",
                    (r["guild_id"], t, r["channel_id"], r["heure"], r["actif"]))
        await self.conn.execute("DROP TABLE edt_config")

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

    # ── Emploi du temps ───────────────────────────────────────────────────────
    @staticmethod
    def _regle(r) -> EdtRegle:
        return EdtRegle(r["guild_id"], r["type"], r["channel_id"], r["heure"], r["jour_semaine"],
                        bool(r["actif"]), r["dernier_envoi"])

    async def edt_regles(self, guild_id: int | None = None) -> list[EdtRegle]:
        sql, args = ("SELECT * FROM edt_regle", ()) if guild_id is None else (
            "SELECT * FROM edt_regle WHERE guild_id=?", (guild_id,))
        async with self.conn.execute(sql, args) as cur:
            return [self._regle(r) for r in await cur.fetchall()]

    async def edt_regle(self, guild_id: int, type_: str) -> EdtRegle | None:
        async with self.conn.execute("SELECT * FROM edt_regle WHERE guild_id=? AND type=?", (guild_id, type_)) as cur:
            r = await cur.fetchone()
        return self._regle(r) if r else None

    async def set_edt_regle(self, c: EdtRegle) -> None:
        await self.conn.execute(
            """INSERT INTO edt_regle (guild_id, type, channel_id, heure, jour_semaine, actif, dernier_envoi)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(guild_id, type) DO UPDATE SET channel_id=excluded.channel_id, heure=excluded.heure,
                 jour_semaine=excluded.jour_semaine, actif=excluded.actif, dernier_envoi=excluded.dernier_envoi""",
            (c.guild_id, c.type, c.channel_id, c.heure, c.jour_semaine, int(c.actif), c.dernier_envoi),
        )
        await self.conn.commit()

    # ── SoWeSign ─────────────────────────────────────────────────────────────
    def _chiffrer(self, texte: str) -> str:
        if not self.fernet:
            raise RuntimeError("SWS_SECRET absent : impossible de stocker des identifiants")
        return self.fernet.encrypt(texte.encode()).decode()

    def _dechiffrer(self, texte: str | None) -> str | None:
        return self.fernet.decrypt(texte.encode()).decode() if texte else None

    def _sws(self, r) -> SwsCompte:
        sigs = self._dechiffrer(r["signatures"])
        return SwsCompte(r["user_id"], self._dechiffrer(r["code"]), r["nom"], json.loads(sigs) if sigs else [],
                         bool(r["notif"]), json.loads(r["connus"]), r["rappel"], r["cree"])

    async def sws_comptes(self) -> list[SwsCompte]:
        async with self.conn.execute("SELECT * FROM sws_compte") as cur:
            return [self._sws(r) for r in await cur.fetchall()]

    async def sws_compte(self, user_id: int) -> SwsCompte | None:
        async with self.conn.execute("SELECT * FROM sws_compte WHERE user_id=?", (user_id,)) as cur:
            r = await cur.fetchone()
        return self._sws(r) if r else None

    async def set_sws_compte(self, c: SwsCompte) -> None:
        await self.conn.execute(
            """INSERT INTO sws_compte (user_id, code, nom, signatures, notif, connus, rappel, cree)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET code=excluded.code, nom=excluded.nom,
                 signatures=excluded.signatures, notif=excluded.notif, connus=excluded.connus,
                 rappel=excluded.rappel""",
            (c.user_id, self._chiffrer(c.code), c.nom,
             self._chiffrer(json.dumps(c.signatures)) if c.signatures else None,
             int(c.notif), json.dumps(c.connus), c.rappel, c.cree),
        )
        await self.conn.commit()

    async def del_sws_compte(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM sws_compte WHERE user_id=?", (user_id,))
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
