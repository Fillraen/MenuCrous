"""Emploi du temps INSA (ADE) : lecture du flux iCal personnel.

URL du type https://ade-outils.insa-lyon.fr/ADE-Cal:~login!2026-2027:<jeton> (EDT_ICS_URL).
Pas de connexion : le jeton est dans l'URL (à garder secret, comme un mot de passe).

Un événement ADE ressemble à :
  SUMMARY:BDD TP3
  LOCATION:501-02-13 - Salle 4IFA
  DESCRIPTION:\\n[IFA-4-S1-EC-BDD:TP] Base de données décisionnelles\\n()\\n\\nIFA:4:S1::BDD:TP::4IFA\\nNOM Prénom\\n\\n(Exporté le:…)
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from .config import TZ

log = logging.getLogger(__name__)

CACHE_TTL = 30 * 60
_cache: tuple[float, list["Cours"]] | None = None


@dataclass(frozen=True)
class Cours:
    uid: str
    debut: datetime  # heure de Paris
    fin: datetime
    titre: str  # « BDD TP3 »
    intitule: str  # « Base de données décisionnelles »
    type: str  # CM / TD / TP / EV…
    salle: str
    profs: tuple[str, ...]

    @property
    def jour(self) -> date:
        return self.debut.date()

    def empreinte(self) -> str:
        """Ce qui compte pour détecter une modification (horaire, salle, titre)."""
        return hashlib.sha1(f"{self.debut}|{self.fin}|{self.titre}|{self.salle}".encode()).hexdigest()[:12]


# ── Lecture iCal (format simple d'ADE : pas de RRULE, dates en UTC) ─────────────────────
def _deplier(texte: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", texte).splitlines()


def _desechapper(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _date(v: str) -> datetime:
    if v.endswith("Z"):
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(TZ)
    if "T" in v:
        return datetime.strptime(v, "%Y%m%dT%H%M%S").replace(tzinfo=TZ)
    return datetime.strptime(v, "%Y%m%d").replace(tzinfo=TZ)


def parse_ics(texte: str) -> list[Cours]:
    cours: list[Cours] = []
    ev: dict[str, str] | None = None
    for ligne in _deplier(texte):
        if ligne == "BEGIN:VEVENT":
            ev = {}
        elif ligne == "END:VEVENT" and ev is not None:
            try:
                cours.append(_cours(ev))
            except (KeyError, ValueError) as exc:
                log.debug("Événement ignoré (%s) : %s", exc, ev.get("SUMMARY"))
            ev = None
        elif ev is not None and ":" in ligne:
            cle, _, val = ligne.partition(":")
            ev[cle.split(";")[0]] = _desechapper(val)
    return sorted(cours, key=lambda c: c.debut)


def _cours(ev: dict[str, str]) -> Cours:
    desc = [l.strip() for l in ev.get("DESCRIPTION", "").split("\n")]
    intitule, typ = "", ""
    for l in desc:
        m = re.match(r"\[(?P<code>[^\]]*)\]\s*(?P<nom>.*)", l)
        if m:
            intitule = m.group("nom").strip()
            typ = m.group("code").rsplit(":", 1)[-1] if ":" in m.group("code") else ""
            break
    # Enseignants : lignes « NOM Prénom » après le code IFA:4:S1::…, avant « (Exporté le… »
    profs = []
    vu_code = False
    for l in desc:
        if re.match(r"^[A-Z0-9]+:\d", l):
            vu_code = True
            continue
        if vu_code and l and not l.startswith("("):
            profs.append(l)
    titre = ev.get("SUMMARY", "").strip()
    if not typ:
        m = re.search(r"\b(CM|TD|TP|EV|DS|EXAM)\w*", titre)
        typ = m.group(1) if m else ""
    return Cours(
        uid=ev.get("UID", titre), debut=_date(ev["DTSTART"]), fin=_date(ev["DTEND"]),
        titre=titre, intitule=intitule or titre, type=typ,
        salle=", ".join(s.strip() for s in ev.get("LOCATION", "").split(",") if s.strip()),
        profs=tuple(profs),
    )


async def get_cours(url: str, force: bool = False) -> list[Cours]:
    global _cache
    if _cache and not force and time.monotonic() - _cache[0] < CACHE_TTL:
        return _cache[1]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    if "BEGIN:VCALENDAR" not in resp.text:
        raise RuntimeError("Le lien ADE ne renvoie pas un calendrier (lien expiré ?)")
    cours = parse_ics(resp.text)
    _cache = (time.monotonic(), cours)
    return cours


def du_jour(cours: list[Cours], d: date) -> list[Cours]:
    return [c for c in cours if c.jour == d]


def lundi(d: date) -> date:
    return d - timedelta(days=d.weekday())


def de_la_semaine(cours: list[Cours], d: date) -> dict[date, list[Cours]]:
    l = lundi(d)
    return {l + timedelta(days=i): du_jour(cours, l + timedelta(days=i)) for i in range(7)}


# ── Détection des modifications sur les prochains jours ─────────────────────────────────
HORIZON_JOURS = 14


def instantane(cours: list[Cours], depuis: date) -> dict:
    fin = depuis + timedelta(days=HORIZON_JOURS)
    return {"fin": fin.isoformat(), "cours": {
        c.uid: {"e": c.empreinte(), "debut": c.debut.isoformat(), "fin": c.fin.isoformat(),
                "titre": c.titre, "salle": c.salle}
        for c in cours if depuis <= c.jour < fin
    }}


@dataclass
class Changement:
    nature: str  # "ajout", "annulation", "modification"
    titre: str
    avant: dict | None
    apres: Cours | None


def changements(snap: dict, cours: list[Cours], depuis: date) -> list[Changement]:
    """Différences avec le relevé précédent, limitées à la fenêtre que ce relevé couvrait
    (un cours qui entre simplement dans la fenêtre de 14 jours n'est pas un « ajout »)."""
    avant: dict[str, dict] = snap.get("cours", {})
    fin_avant = date.fromisoformat(snap["fin"])
    apres = {c.uid: c for c in cours if depuis <= c.jour < depuis + timedelta(days=HORIZON_JOURS)}
    out: list[Changement] = []
    for uid, c in apres.items():
        old = avant.get(uid)
        if old is None:
            if c.jour < fin_avant:
                out.append(Changement("ajout", c.titre, None, c))
        elif old["e"] != c.empreinte():
            out.append(Changement("modification", c.titre, old, c))
    for uid, old in avant.items():
        if uid in apres or datetime.fromisoformat(old["debut"]).date() < depuis:
            continue
        ailleurs = next((c for c in cours if c.uid == uid), None)
        if ailleurs:  # déplacé au-delà de la fenêtre
            out.append(Changement("modification", ailleurs.titre, old, ailleurs))
        else:  # disparu du flux alors qu'il n'était pas encore passé
            out.append(Changement("annulation", old["titre"], old, None))
    return out
