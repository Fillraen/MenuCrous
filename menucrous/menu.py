"""Récupération des menus CROUS.

Source principale : la page du restaurant sur crous-lyon.fr (HTML statique, pas besoin de
navigateur : les menus de la semaine sont déjà dans la page, un bloc ``div.menu`` par jour).
Secours : l'API publique de CROUStillant (https://api.croustillant.menu), utilisée quand le
site du CROUS ne répond pas ou n'a pas le jour demandé.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

from .config import TZ, Restaurant

log = logging.getLogger(__name__)

UA = "MenuCrous/2.0 (bot Discord ; https://github.com/Fillraen/MenuCrous)"
CACHE_TTL = 15 * 60  # secondes
CROUSTILLANT_API = "https://api.croustillant.menu/v1"

# Catégories masquées en mode « court » (le mode complet les affiche)
_COURT_EXCLUS = ("entrée", "entree", "dessert")


@dataclass
class Categorie:
    nom: str
    plats: list[str]


@dataclass
class Repas:
    nom: str  # « Déjeuner », « Dîner »…
    categories: list[Categorie]


@dataclass
class MenuJour:
    restaurant: Restaurant
    jour: date
    repas: list[Repas]
    source: str  # "crous" ou "croustillant"

    @property
    def vide(self) -> bool:
        return not any(c.plats for r in self.repas for c in r.categories)


@dataclass
class _Cache:
    at: float
    menus: dict[date, list[Repas]] = field(default_factory=dict)


_cache: dict[tuple[str, str], _Cache] = {}


def _nettoie(texte: str) -> str:
    return re.sub(r"\s+", " ", texte).strip()


# ── Source 1 : crous-lyon.fr ────────────────────────────────────────────────────────────
def parse_crous(html: str) -> dict[date, list[Repas]]:
    soup = BeautifulSoup(html, "html.parser")
    menus: dict[date, list[Repas]] = {}
    for bloc in soup.select("div.menus div.menu"):
        t = bloc.select_one(".menu_date_title time[datetime]")
        if not t:
            continue
        try:
            jour = date.fromisoformat(t["datetime"][:10])
        except ValueError:
            continue
        repas: list[Repas] = []
        for meal in bloc.select("div.meal"):
            titre = meal.select_one(".meal_title")
            cats: list[Categorie] = []
            ul = meal.select_one("ul.meal_foodies")
            for li in ul.find_all("li", recursive=False) if ul else []:
                nom = _nettoie("".join(li.find_all(string=True, recursive=False)))
                plats = [_nettoie(p.get_text()) for p in li.select("ul > li")]
                plats = [p for p in plats if p]
                if nom or plats:
                    cats.append(Categorie(nom or "Autre", plats))
            repas.append(Repas(_nettoie(titre.get_text()) if titre else "Repas", cats))
        menus[jour] = repas
    return menus


async def _fetch_crous(client: httpx.AsyncClient, r: Restaurant) -> dict[date, list[Repas]]:
    resp = await client.get(r.crous_url)
    resp.raise_for_status()
    return parse_crous(resp.text)


# ── Source 2 : API CROUStillant ─────────────────────────────────────────────────────────
_REPAS_NOMS = {"matin": "Petit-déjeuner", "midi": "Déjeuner", "soir": "Dîner"}


def parse_croustillant(data: list[dict], restaurant_key: str = "") -> dict[date, list[Repas]]:
    menus: dict[date, list[Repas]] = {}
    for j in data:
        jour = datetime.strptime(j["date"], "%d-%m-%Y").date()
        _types_plats[(restaurant_key, jour)] = {
            _norm(p["libelle"]): p["analyse"]["type"]
            for rp in j.get("repas", []) for c in rp.get("categories", []) for p in c.get("plats", [])
            if (p.get("analyse") or {}).get("type")
        }
        repas = []
        for rp in j.get("repas", []):
            cats = [
                Categorie(
                    _nettoie(c.get("libelle") or "Autre"),
                    [_nettoie(p["libelle"]) for p in sorted(c.get("plats", []), key=lambda p: p.get("ordre", 0))],
                )
                for c in sorted(rp.get("categories", []), key=lambda c: c.get("ordre", 0))
            ]
            repas.append(Repas(_REPAS_NOMS.get(rp.get("type"), rp.get("type", "Repas")), cats))
        menus[jour] = repas
    return menus


async def _fetch_croustillant(client: httpx.AsyncClient, r: Restaurant) -> dict[date, list[Repas]]:
    resp = await client.get(f"{CROUSTILLANT_API}/restaurants/{r.croustillant_id}/menu")
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"CROUStillant : {payload}")
    return parse_croustillant(payload["data"], r.key)


_SOURCES = {"crous": _fetch_crous, "croustillant": _fetch_croustillant}

# ── Reclassement des catégories ─────────────────────────────────────────────────────────
# Certains restaurants (Puvis) saisissent leurs plats dans la mauvaise catégorie (desserts
# sous « Plat végétarien », plats sous « Dessert »). CROUStillant analyse chaque plat
# (ENTREE / PLAT / ACCOMPAGNEMENT / DESSERT) : on s'en sert pour remettre d'aplomb.
_types_plats: dict[tuple[str, date], dict[str, str]] = {}
_ATTENDU = {"entr": "ENTREE", "dessert": "DESSERT", "plat": "PLAT", "accomp": "ACCOMPAGNEMENT"}
_LIBELLE = {"ENTREE": "Entrée", "DESSERT": "Dessert", "PLAT": "Plat", "ACCOMPAGNEMENT": "Accompagnement"}


def _norm(libelle: str) -> str:
    return re.sub(r"[^a-z0-9]", "", libelle.lower())


def _type_attendu(nom: str) -> str | None:
    n = nom.lower()
    return next((t for k, t in _ATTENDU.items() if n.startswith(k)), None)


def _type_dominant(c: Categorie, types: dict[str, str]) -> str | None:
    vus = [types[_norm(p)] for p in c.plats if _norm(p) in types]
    if len(vus) < max(1, len(c.plats) // 2):
        return None
    top = max(set(vus), key=vus.count)
    return top if vus.count(top) * 3 >= len(vus) * 2 else None  # ≥ 2/3 d'accord


def reclasse(repas: list[Repas], types: dict[str, str]) -> list[Repas]:
    if not types:
        return repas
    out = []
    for rp in repas:
        cats = list(rp.categories)
        faux = {}  # index -> type réel, pour les catégories dont le nom ne colle pas
        for i, c in enumerate(cats):
            attendu, reel = _type_attendu(c.nom), _type_dominant(c, types)
            # Accompagnement / plat : frontière floue chez le CROUS, on ne touche pas.
            if attendu and reel and attendu != reel and {attendu, reel} != {"PLAT", "ACCOMPAGNEMENT"}:
                faux[i] = reel
        noms = {i: cats[i].nom for i in faux}
        for i, reel in faux.items():
            # Échange de noms entre deux catégories inversées (cas Puvis) sinon nom générique
            j = next((j for j, r in faux.items() if j != i and _type_attendu(noms[j]) == reel), None)
            cats[i] = Categorie(noms[j] if j is not None else _LIBELLE[reel], cats[i].plats)
        out.append(Repas(rp.nom, cats))
    return out


async def _menus_source(r: Restaurant, source: str, force: bool = False) -> dict[date, list[Repas]]:
    cle = (r.key, source)
    c = _cache.get(cle)
    if c and not force and time.monotonic() - c.at < CACHE_TTL:
        return c.menus
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, headers={"User-Agent": UA}
    ) as client:
        menus = await _SOURCES[source](client, r)
    _cache[cle] = _Cache(time.monotonic(), menus)
    return menus


async def _avec_types(r: Restaurant, m: MenuJour) -> MenuJour:
    if (r.key, m.jour) not in _types_plats:
        try:
            await _menus_source(r, "croustillant")
        except Exception:
            return m
    m.repas = reclasse(m.repas, _types_plats.get((r.key, m.jour), {}))
    return m


async def get_menu(r: Restaurant, jour: date, force: bool = False) -> MenuJour | None:
    """Menu d'un restaurant pour un jour : CROUS d'abord, CROUStillant en secours.

    Renvoie None si aucune des deux sources n'a de menu pour ce jour (restaurant fermé).
    Lève RuntimeError si les deux sources sont injoignables.
    """
    erreurs = []
    for source in ("crous", "croustillant"):
        try:
            menus = await _menus_source(r, source, force)
        except Exception as exc:  # réseau, HTML/JSON inattendu…
            log.warning("Menu %s via %s : %s", r.key, source, exc)
            erreurs.append(f"{source}: {exc}")
            continue
        m = MenuJour(r, jour, menus.get(jour, []), source)
        if not m.vide:
            return await _avec_types(r, m)
    if len(erreurs) == len(_SOURCES):
        raise RuntimeError("; ".join(erreurs))
    return None


async def get_semaine(r: Restaurant) -> list[MenuJour]:
    """Tous les jours connus à partir d'aujourd'hui (union des deux sources)."""
    today = datetime.now(TZ).date()
    jours: dict[date, MenuJour] = {}
    for source in ("croustillant", "crous"):  # crous en dernier : il a la priorité
        try:
            menus = await _menus_source(r, source)
        except Exception as exc:
            log.warning("Semaine %s via %s : %s", r.key, source, exc)
            continue
        for d, repas in menus.items():
            m = MenuJour(r, d, repas, source)
            if d >= today and not m.vide:
                jours[d] = m
    return [await _avec_types(r, jours[d]) for d in sorted(jours)]


def filtre_court(m: MenuJour) -> MenuJour:
    """Retire entrées et desserts (mode par défaut, plus lisible sur Discord)."""
    repas = [
        Repas(rp.nom, [c for c in rp.categories if not c.nom.lower().startswith(_COURT_EXCLUS)])
        for rp in m.repas
    ]
    return MenuJour(m.restaurant, m.jour, repas, m.source)


async def get_infos(r: Restaurant) -> dict:
    """Horaires, adresse, accès… (CROUStillant)."""
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as client:
        resp = await client.get(f"{CROUSTILLANT_API}/restaurants/{r.croustillant_id}")
        resp.raise_for_status()
        return resp.json().get("data", {})
