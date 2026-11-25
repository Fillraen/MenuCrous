"""Mise en forme Discord (embeds) des menus et de l'emploi du temps."""
from __future__ import annotations

from datetime import date, datetime

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


# ── Emploi du temps ─────────────────────────────────────────────────────────────────────
_EMOJI_TYPE = {"CM": "🎓", "TD": "✏️", "TP": "💻", "EV": "📝", "DS": "📝", "EXAM": "📝"}
EDT_COULEUR = 0x2ECC71


def _h(dt) -> str:
    return f"{dt.hour}h{dt.minute:02d}" if dt.minute else f"{dt.hour}h"


def _duree(cours) -> str:
    minutes = sum((c.fin - c.debut).seconds // 60 for c in cours)
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


def _salle_courte(salle: str) -> str:
    # « 501-02-13 - Salle 4IFA » → « Salle 4IFA (501-02-13) »
    parts = []
    for s in salle.split(", "):
        code, _, nom = s.partition(" - ")
        parts.append(f"{nom} ({code})" if nom else code)
    return ", ".join(parts)


def edt_jour_embed(d: date, cours: list) -> discord.Embed:
    if not cours:
        return discord.Embed(title=f"📅 {jour_fr(d).capitalize()}", description="😴 Pas de cours.",
                             color=0x7F8C8D)
    e = discord.Embed(
        title=f"📅 {jour_fr(d).capitalize()} — {len(cours)} cours · {_duree(cours)}",
        color=EDT_COULEUR,
    )
    for c in cours:
        details = [c.intitule] if c.intitule != c.titre else []
        if c.salle:
            details.append(f"📍 {_salle_courte(c.salle)}")
        if c.profs:
            details.append(f"👤 {', '.join(c.profs)}")
        e.add_field(
            name=f"{_EMOJI_TYPE.get(c.type, '📘')} {_h(c.debut)} – {_h(c.fin)} · {c.titre}",
            value="\n".join(details)[:1024] or "​", inline=False,
        )
    return e


def edt_semaine_embed(jours: dict, titre: str = "") -> discord.Embed:
    """jours = {date: [Cours]} du lundi au dimanche."""
    dates = list(jours)
    tous = [c for l in jours.values() for c in l]
    titre = titre or f"🗓️ Semaine du {jour_fr(dates[0]).split(' ', 1)[1]} au {jour_fr(dates[4]).split(' ', 1)[1]}"
    if not tous:
        return discord.Embed(title=titre, color=0x7F8C8D,
                             description="🏢 Aucun cours cette semaine : période en entreprise (ou vacances).")
    e = discord.Embed(title=titre, color=EDT_COULEUR,
                      description=f"**{len(tous)}** cours · **{_duree(tous)}** au total")
    for d, cours in jours.items():
        if d.weekday() >= 5 and not cours:
            continue  # week-end vide : on n'affiche pas
        if not cours:
            valeur = "—"
        else:
            valeur = "\n".join(
                f"`{_h(c.debut)}–{_h(c.fin)}` {_EMOJI_TYPE.get(c.type, '📘')} **{c.titre}**"
                + (f" · {_salle_courte(c.salle).split(' (')[0]}" if c.salle else "")
                for c in cours
            )
        e.add_field(name=jour_fr(d).capitalize(), value=valeur[:1024], inline=False)
    return e


def edt_changements_embed(chg: list) -> discord.Embed:
    lignes = []
    for c in chg[:25]:
        if c.nature == "ajout":
            n = c.apres
            lignes.append(f"➕ **{n.titre}** ajouté : {jour_fr(n.jour)} {_h(n.debut)}–{_h(n.fin)}"
                          + (f" · {_salle_courte(n.salle).split(' (')[0]}" if n.salle else ""))
        elif c.nature == "annulation":
            deb = datetime.fromisoformat(c.avant["debut"])
            lignes.append(f"❌ **{c.titre}** du {jour_fr(deb.date())} {_h(deb)} n'apparaît plus")
        else:
            a, n = c.avant, c.apres
            deb = datetime.fromisoformat(a["debut"])
            quoi = []
            if a["debut"] != n.debut.isoformat() or a["fin"] != n.fin.isoformat():
                quoi.append(f"{jour_fr(deb.date())} {_h(deb)} → **{jour_fr(n.jour)} {_h(n.debut)}–{_h(n.fin)}**")
            if a["salle"] != n.salle:
                quoi.append(f"salle → **{_salle_courte(n.salle) or '?'}**")
            if a["titre"] != n.titre:
                quoi.append(f"devient **{n.titre}**")
            lignes.append(f"🔄 **{a['titre']}** : " + ", ".join(quoi or ["modifié"]))
    desc = "\n".join(lignes)
    return discord.Embed(title="📌 Emploi du temps modifié", color=0xF39C12,
                         description=desc[:4000]).set_footer(text="Sur les 14 prochains jours")
