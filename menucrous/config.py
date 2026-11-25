"""Configuration lue dans l'environnement (fichier .env en local, env du conteneur en prod)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Paris"))


@dataclass(frozen=True)
class Restaurant:
    key: str  # identifiant court utilisé dans les commandes et en base
    nom: str
    crous_url: str  # source principale (site officiel du CROUS)
    croustillant_id: int  # source de secours (API CROUStillant)

    @property
    def croustillant_url(self) -> str:
        return f"https://croustillant.menu/fr/restaurants/r{self.croustillant_id}"


RESTAURANTS: dict[str, Restaurant] = {
    "manufacture": Restaurant(
        key="manufacture",
        nom="Manufacture des Tabacs",
        crous_url="https://www.crous-lyon.fr/restaurant/restaurant-manufacture-des-tabacs/",
        croustillant_id=620,
    ),
    "puvis": Restaurant(
        key="puvis",
        nom="Puvis de Chavannes",
        crous_url="https://www.crous-lyon.fr/restaurant/restaurant-puvis-de-chavannes-2/",
        croustillant_id=2265,
    ),
}


def _int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Variable {name} invalide (entier attendu) : {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    discord_token: str
    data_dir: Path
    # Valeurs par défaut appliquées au premier démarrage (ensuite : /config dans Discord)
    default_menu_channel: int | None
    default_menu_time: str
    default_notes_channel: int | None
    # Mon Dossier Web (INSA) — un seul compte surveillé : celui du propriétaire
    insa_username: str
    insa_password: str
    insa_totp_secret: str  # clé du QR code 2FA (facultatif) : codes générés sans toi
    notes_owner_id: int | None
    notes_url: str
    notes_interval_min: int
    # Emploi du temps ADE (flux iCal personnel, le jeton est dans l'URL)
    edt_ics_url: str
    # Petite page web publique (menu de la semaine + /health)
    web_port: int
    public_url: str

    @property
    def notes_enabled(self) -> bool:
        return bool(self.insa_username and self.insa_password)


def load_settings() -> Settings:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Variable d'environnement DISCORD_TOKEN manquante.")
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        discord_token=token,
        data_dir=data_dir,
        # CHANNEL_ID = ancien nom (version Railway), toujours accepté
        default_menu_channel=_int("MENU_CHANNEL_ID") or _int("CHANNEL_ID"),
        default_menu_time=os.environ.get("MENU_TIME", "08:00"),
        default_notes_channel=_int("NOTES_CHANNEL_ID"),
        insa_username=os.environ.get("INSA_USERNAME", "").strip(),
        insa_password=os.environ.get("INSA_PASSWORD", ""),
        insa_totp_secret=os.environ.get("INSA_TOTP_SECRET", "").strip(),
        notes_owner_id=_int("NOTES_OWNER_ID"),
        notes_url=os.environ.get(
            "NOTES_URL", "https://mondossierweb.insa-lyon.fr/mondossierweb/inscriptions"
        ),
        notes_interval_min=_int("NOTES_INTERVAL_MIN", 30) or 30,
        edt_ics_url=os.environ.get("EDT_ICS_URL", "").strip(),
        web_port=_int("WEB_PORT", 8080) or 8080,
        public_url=os.environ.get("PUBLIC_URL", "").rstrip("/"),
    )
