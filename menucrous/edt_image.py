"""Rendu de l'emploi du temps de la semaine en image (grille lundi → vendredi), avec Pillow.

Thème sombre proche de Discord. Une couleur stable par matière (hash du préfixe du titre :
« BDD CM1 », « BDD TP3 » → même couleur). Cours qui se chevauchent : côte à côte.
"""
from __future__ import annotations

import hashlib
import io
from datetime import date

from PIL import Image, ImageDraw, ImageFont

from .format import JOURS, MOIS

S = 2  # facteur d'échelle (image nette sur écrans haute densité)
LARG_HEURES = 56 * S
LARG_JOUR = 250 * S
HAUT_ENTETE = 54 * S
HAUT_HEURE = 64 * S
MARGE = 14 * S

FOND = (30, 31, 34)
FOND_COL = (43, 45, 49)
LIGNE = (63, 65, 71)
TEXTE = (235, 236, 240)
TEXTE_DOUX = (160, 163, 172)
AUJOURDHUI = (88, 101, 242)

# Palette lisible sur fond sombre (texte blanc dessus)
PALETTE = [  # teintes bien séparées sur le cercle chromatique
    (66, 133, 244), (234, 67, 53), (52, 168, 83), (251, 188, 5), (171, 71, 188),
    (0, 172, 193), (255, 112, 67), (124, 179, 66), (236, 64, 122), (92, 107, 192),
    (141, 110, 99), (38, 166, 154),
]
TYPES = {"CM": "CM", "TD": "TD", "TP": "TP", "EV": "EXAM", "DS": "EXAM", "EXAM": "EXAM"}

_POLICES = {
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf"],
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/segoeuib.ttf",
           "C:/Windows/Fonts/arialbd.ttf"],
}


def _police(taille: int, gras: bool = False) -> ImageFont.FreeTypeFont:
    for chemin in _POLICES[gras]:
        try:
            return ImageFont.truetype(chemin, taille * S)
        except OSError:
            continue
    return ImageFont.load_default(taille * S)


def _matiere(titre: str) -> str:
    return titre.split()[0] if titre else "?"


def _couleurs(cours: list) -> dict[str, tuple[int, int, int]]:
    """Une couleur par matière : stable (hash) si possible, jamais deux fois la même dans l'image."""
    prises: dict[str, tuple[int, int, int]] = {}
    for m in sorted({_matiere(c.titre) for c in cours}):
        i = int(hashlib.md5(m.encode()).hexdigest(), 16) % len(PALETTE)
        for k in range(len(PALETTE)):
            col = PALETTE[(i + k) % len(PALETTE)]
            if col not in prises.values() or len(prises) >= len(PALETTE):
                prises[m] = col
                break
    return prises


def _assombrir(c, f=0.55):
    return tuple(int(x * f) for x in c)


def _couper(draw: ImageDraw.ImageDraw, texte: str, police, largeur: int, max_lignes: int) -> list[str]:
    mots, lignes, cur = texte.split(), [], ""
    for m in mots:
        essai = f"{cur} {m}".strip()
        if draw.textlength(essai, font=police) <= largeur:
            cur = essai
        else:
            if cur:
                lignes.append(cur)
            cur = m
        if len(lignes) == max_lignes:
            break
    if cur and len(lignes) < max_lignes:
        lignes.append(cur)
    if len(lignes) == max_lignes and " ".join(lignes) != " ".join(mots):
        while lignes[-1] and draw.textlength(lignes[-1] + "…", font=police) > largeur:
            lignes[-1] = lignes[-1][:-1]
        lignes[-1] += "…"
    return lignes


def _couloirs(cours: list) -> list[tuple[object, int, int]]:
    """Répartit les cours qui se chevauchent : (cours, index du couloir, nb de couloirs)."""
    out, groupe, fin_groupe = [], [], None

    def vider():
        couloirs: list = []  # heure de fin de chaque couloir
        places = []
        for c in groupe:
            i = next((k for k, f in enumerate(couloirs) if f <= c.debut), None)
            if i is None:
                couloirs.append(c.fin)
                i = len(couloirs) - 1
            else:
                couloirs[i] = c.fin
            places.append((c, i))
        out.extend((c, i, len(couloirs)) for c, i in places)

    for c in sorted(cours, key=lambda c: c.debut):
        if fin_groupe is not None and c.debut >= fin_groupe:
            vider()
            groupe, fin_groupe = [], None
        groupe.append(c)
        fin_groupe = max(fin_groupe, c.fin) if fin_groupe else c.fin
    if groupe:
        vider()
    return out


def rendre_semaine(jours: dict[date, list], aujourd_hui: date | None = None) -> bytes:
    """jours = {date: [Cours]} du lundi au dimanche → PNG (week-end affiché seulement s'il a des cours)."""
    dates = [d for d in jours if d.weekday() < 5 or jours[d]]
    tous = [c for d in dates for c in jours[d]]
    h_min = min([c.debut.hour for c in tous] + [8])
    h_max = max([c.fin.hour + (1 if c.fin.minute else 0) for c in tous] + [18])
    largeur = LARG_HEURES + len(dates) * LARG_JOUR + MARGE * 2
    hauteur = HAUT_ENTETE + (h_max - h_min) * HAUT_HEURE + MARGE * 2

    img = Image.new("RGB", (largeur, hauteur), FOND)
    dr = ImageDraw.Draw(img)
    f_jour, f_date = _police(15, True), _police(12)
    f_heure, f_titre, f_txt, f_badge = _police(11), _police(13, True), _police(11), _police(9, True)

    x0, y0 = MARGE + LARG_HEURES, MARGE + HAUT_ENTETE
    couleurs = _couleurs(tous)

    def y_de(dt) -> int:
        return y0 + int(((dt.hour - h_min) + dt.minute / 60) * HAUT_HEURE)

    # Colonnes des jours + en-têtes
    for i, d in enumerate(dates):
        x = x0 + i * LARG_JOUR
        dr.rounded_rectangle([x + 3 * S, y0, x + LARG_JOUR - 3 * S, hauteur - MARGE], 8 * S, fill=FOND_COL)
        couleur_entete = AUJOURDHUI if d == aujourd_hui else TEXTE
        dr.text((x + LARG_JOUR / 2, MARGE + 14 * S), JOURS[d.weekday()].capitalize(), font=f_jour,
                fill=couleur_entete, anchor="mm")
        dr.text((x + LARG_JOUR / 2, MARGE + 36 * S), f"{d.day} {MOIS[d.month - 1]}", font=f_date,
                fill=TEXTE_DOUX, anchor="mm")
        if not jours[d]:
            dr.text((x + LARG_JOUR / 2, (y0 + hauteur) / 2), "Pas de cours", font=f_txt,
                    fill=TEXTE_DOUX, anchor="mm")

    # Lignes des heures
    for h in range(h_min, h_max + 1):
        y = y0 + (h - h_min) * HAUT_HEURE
        dr.line([x0, y, largeur - MARGE, y], fill=LIGNE, width=S)
        dr.text((x0 - 8 * S, y), f"{h}h", font=f_heure, fill=TEXTE_DOUX, anchor="rm")

    # Cours
    for i, d in enumerate(dates):
        xc = x0 + i * LARG_JOUR + 6 * S
        lc = LARG_JOUR - 12 * S
        for c, couloir, n in _couloirs(jours[d]):
            w = lc // n
            x1, x2 = xc + couloir * w + 2 * S, xc + (couloir + 1) * w - 2 * S
            y1, y2 = y_de(c.debut) + 2 * S, y_de(c.fin) - 2 * S
            col = couleurs[_matiere(c.titre)]
            dr.rounded_rectangle([x1, y1, x2, y2], 7 * S, fill=_assombrir(col))
            dr.rounded_rectangle([x1, y1, x1 + 5 * S, y2], 3 * S, fill=col)  # liseré à gauche
            tx, ty, tw = x1 + 11 * S, y1 + 6 * S, x2 - x1 - 16 * S

            badge = TYPES.get(c.type, "")
            if badge:
                bw = dr.textlength(badge, font=f_badge) + 10 * S
                dr.rounded_rectangle([x2 - bw - 5 * S, ty, x2 - 5 * S, ty + 16 * S], 4 * S, fill=col)
                dr.text((x2 - bw / 2 - 5 * S, ty + 8 * S), badge, font=f_badge, fill=(255, 255, 255),
                        anchor="mm")
                tw_titre = tw - bw - 6 * S
            else:
                tw_titre = tw
            dr.text((tx, ty), _couper(dr, c.titre, f_titre, tw_titre, 1)[0], font=f_titre, fill=TEXTE)
            ty += 20 * S
            infos = [f"{c.debut:%H:%M} – {c.fin:%H:%M}"]
            if c.salle:
                infos.append(c.salle.split(", ")[0].split(" - ")[-1])
            if c.intitule and c.intitule != c.titre:
                infos += _couper(dr, c.intitule, f_txt, tw, 2)
            for ligne in infos:
                if ty + 15 * S > y2 - 4 * S:
                    break
                dr.text((tx, ty), _couper(dr, ligne, f_txt, tw, 1)[0], font=f_txt,
                        fill=TEXTE if ligne == infos[0] else TEXTE_DOUX)
                ty += 16 * S

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
