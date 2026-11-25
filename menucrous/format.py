"""Mise en forme Discord (embeds) des menus."""
from __future__ import annotations

from datetime import date

import discord

from .menu import MenuJour

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

_ORDRE = ["entr", "plat viande", "plat", "accomp", "dessert"]
_EMOJI = {"entr": "🥗", "plat viande": "🍖", "plat v": "🥦", "plat": "🍛", "accomp": "🍚", "dessert": "🍰"}
_COULEURS = {"manufacture": 0xE67E22, "puvis": 0x3498DB}


def jour_fr(d: date) -> str:
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"


def _rang(nom: str) -> int:
    n = nom.lower()
    return next((i for i, k in enumerate(_ORDRE) if n.startswith(k)), len(_ORDRE))


def _emoji(nom: str) -> str:
    n = nom.lower()
    return next((e for k, e in _EMOJI.items() if n.startswith(k)), "🍴")


def menu_embed(m: MenuJour, complet: bool) -> discord.Embed:
    r = m.restaurant
    e = discord.Embed(
        title=f"🍽️ {r.nom} — {jour_fr(m.jour)}",
        url=r.crous_url,
        color=_COULEURS.get(r.key, 0x95A5A6),
    )
    plusieurs_repas = len(m.repas) > 1
    for rp in m.repas:
        if plusieurs_repas:
            e.add_field(name=f"━━ {rp.nom} ━━", value="​", inline=False)
        for c in sorted(rp.categories, key=lambda c: _rang(c.nom)):
            if not c.plats:
                continue
            valeur = "\n".join(f"• {p}" for p in c.plats)
            if len(valeur) > 1024:
                valeur = valeur[:1020] + "…"
            e.add_field(name=f"{_emoji(c.nom)} {c.nom}", value=valeur, inline=False)
    pied = "Source : site du CROUS" if m.source == "crous" else "Source : CROUStillant (secours)"
    if not complet:
        pied += " · entrées et desserts masqués (/menu complet:True)"
    e.set_footer(text=pied)
    return e


def ferme_embed(nom: str, d: date) -> discord.Embed:
    return discord.Embed(
        title=f"📭 {nom} — {jour_fr(d)}",
        description="Pas de menu publié (restaurant fermé, week-end, ou menu pas encore en ligne).",
        color=0x7F8C8D,
    )
