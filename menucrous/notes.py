"""Surveillance des notes sur Mon Dossier Web (INSA Lyon).

Mon Dossier Web est une appli Vaadin (tout est rendu en JavaScript) derrière le SSO Keycloak
de l'INSA (CAS) : on passe donc par un vrai navigateur headless (Playwright/Chromium).

Un seul compte est surveillé : celui du propriétaire du bot (INSA_USERNAME / INSA_PASSWORD).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

log = logging.getLogger(__name__)

LOGIN_HOST = "idauth.insa-lyon.fr"
NAV_TIMEOUT = 45_000


class NotesError(RuntimeError):
    pass


class LoginError(NotesError):
    pass


@dataclass
class Note:
    cle: str  # identifiant stable (hash de l'année + code + libellé)
    annee: str
    code: str
    matiere: str
    valeur: str  # "" tant que la note n'est pas publiée
    resultat: str = ""

    @property
    def publiee(self) -> bool:
        return bool(self.valeur.strip())

    def to_dict(self) -> dict:
        return asdict(self)


def _cle(*parts: str) -> str:
    return hashlib.sha1("|".join(p.strip().lower() for p in parts).encode()).hexdigest()[:16]


def _propre(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


async def _login(page: Page, username: str, password: str) -> None:
    if LOGIN_HOST not in page.url:
        return  # session déjà valide
    await page.fill("#username", username)
    await page.fill("#password", password)
    await page.click("#kc-login")
    try:
        await page.wait_for_url(lambda u: LOGIN_HOST not in u, timeout=NAV_TIMEOUT)
    except PWTimeout:
        err = await page.locator("#input-error, .pf-m-error, .alert-error, [id*=error]").all_inner_texts()
        raise LoginError(_propre(" ".join(err)) or "Connexion INSA refusée (identifiants ? MFA ?)")


async def _extraire(page: Page) -> list[Note]:
    """Lit le tableau des notes rendu par Vaadin.

    Stratégie volontairement tolérante : on parcourt toutes les lignes de grille / tableau
    visibles, et on garde celles qui ressemblent à « code | libellé | … | note | résultat ».
    """
    rows: list[list[str]] = await page.evaluate(
        """() => {
            const out = [];
            // vaadin-grid : les cellules sont des <vaadin-grid-cell-content> en light DOM,
            // dans l'ordre ligne par ligne ; on reconstitue via les slots des <td> du shadow DOM.
            for (const grid of document.querySelectorAll('vaadin-grid, vaadin-tree-grid')) {
              const body = grid.shadowRoot && grid.shadowRoot.querySelector('tbody#items');
              if (!body) continue;
              for (const tr of body.querySelectorAll('tr')) {
                if (tr.hidden) continue;
                const cells = [...tr.querySelectorAll('td')].map(td => {
                  const slot = td.querySelector('slot');
                  const els = slot ? slot.assignedElements({flatten: true}) : [];
                  return els.map(e => e.innerText).join(' ').trim();
                });
                if (cells.some(c => c)) out.push(cells);
              }
            }
            // Tables HTML classiques (au cas où)
            for (const tr of document.querySelectorAll('table tr')) {
              const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
              if (cells.length >= 2 && cells.some(c => c)) out.push(cells);
            }
            return out;
        }"""
    )
    notes: list[Note] = []
    annee = ""
    for cells in rows:
        cells = [_propre(c) for c in cells]
        m = next((c for c in cells if re.fullmatch(r"20\d\d[/-]20\d\d", c)), None)
        if m:
            annee = m
        texte = [c for c in cells if c]
        if len(texte) < 2:
            continue
        # La note = dernière cellule numérique de la forme 12,5 / 12.5 / 12,5/20 / ABI / ABJ
        valeur = ""
        for c in reversed(cells):
            if re.fullmatch(r"\d{1,2}([.,]\d{1,3})?(\s*/\s*20)?|AB[IJ]|DEF|ABS", c, re.I):
                valeur = c
                break
        code = cells[0] if re.fullmatch(r"[A-Z0-9][A-Z0-9_\-]{2,}", cells[0] or "") else ""
        matiere = next(
            (c for c in cells if c != code and len(c) > 3 and not re.fullmatch(r"[\d.,/\s]+", c)), texte[0]
        )
        resultat = next((c for c in cells if c.upper() in {"ADM", "AJ", "ADJ", "VAL", "NVAL", "ADMIS", "AJOURNÉ"}), "")
        notes.append(Note(_cle(annee, code, matiere), annee, code, matiere, valeur, resultat))
    # dédoublonnage (une même ligne peut sortir via la grille ET un tableau)
    vues: dict[str, Note] = {}
    for n in notes:
        if n.cle not in vues or (n.publiee and not vues[n.cle].publiee):
            vues[n.cle] = n
    return list(vues.values())


async def fetch_notes(
    username: str, password: str, url: str, debug_dir: Path | None = None
) -> list[Note]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            ctx = await browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
            page = await ctx.new_page()
            page.set_default_timeout(NAV_TIMEOUT)
            await page.goto(url, wait_until="domcontentloaded")
            await _login(page, username, password)
            if page.url.rstrip("/") != url.rstrip("/"):
                await page.goto(url, wait_until="domcontentloaded")
            # Vaadin : attendre que la grille ait des lignes
            try:
                await page.wait_for_function(
                    """() => [...document.querySelectorAll('vaadin-grid, vaadin-tree-grid, table')]
                           .some(g => (g.shadowRoot ? g.shadowRoot.querySelectorAll('tbody#items tr').length
                                                   : g.querySelectorAll('tr').length) > 0)""",
                    timeout=NAV_TIMEOUT,
                )
            except PWTimeout:
                pass
            await page.wait_for_timeout(1500)
            notes = await _extraire(page)
            if debug_dir is not None:
                debug_dir.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(debug_dir / "notes.png"), full_page=True)
                (debug_dir / "notes.html").write_text(await page.content(), encoding="utf-8")
            if not notes:
                raise NotesError(f"Aucune note trouvée sur {page.url} (page changée ?)")
            return notes
        finally:
            await browser.close()


def diff(avant: dict[str, dict], apres: list[Note]) -> list[Note]:
    """Notes publiées depuis le dernier passage (nouvelle ligne avec note, ou note apparue)."""
    nouvelles = []
    for n in apres:
        old = avant.get(n.cle)
        if n.publiee and (old is None or old.get("valeur", "") != n.valeur):
            nouvelles.append(n)
    return nouvelles


if __name__ == "__main__":  # python -m menucrous.notes : test manuel, sauvegarde debug/
    import os

    from dotenv import load_dotenv

    load_dotenv()
    res = asyncio.run(
        fetch_notes(
            os.environ["INSA_USERNAME"], os.environ["INSA_PASSWORD"],
            os.environ.get("NOTES_URL", "https://mondossierweb.insa-lyon.fr/mondossierweb/notes"),
            Path(os.environ.get("DATA_DIR", "./data")) / "debug",
        )
    )
    for n in res:
        print(n)
