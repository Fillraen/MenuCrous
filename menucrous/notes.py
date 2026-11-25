"""Surveillance des notes sur Mon Dossier Web (INSA Lyon).

Mon Dossier Web (ESUP, Vaadin : tout est rendu en JavaScript) est derrière le SSO Keycloak de
l'INSA : on passe donc par un vrai navigateur headless (Playwright/Chromium).

Parcours de la page (vérifié le 2026-11-25) :
  /mondossierweb/inscriptions → une carte par année d'inscription, bouton « Notes et résultats »
  → fenêtre avec une arborescence (vaadin-tree-grid) :
     niveau 0 année · 1 semestre · 2 UE · 3 matière (EC) · 4 épreuve
  2 colonnes : libellé | « Aucun résultat » ou « 13.95/20 VALIDE » (affiché en double).

Un seul compte est surveillé : celui du propriétaire du bot.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

log = logging.getLogger(__name__)

LOGIN_HOST = "idauth.insa-lyon.fr"
NAV_TIMEOUT = 45_000
URL_DEFAUT = "https://mondossierweb.insa-lyon.fr/mondossierweb/inscriptions"

NIV_ANNEE, NIV_SEMESTRE, NIV_UE, NIV_MATIERE, NIV_EPREUVE = range(5)


class NotesError(RuntimeError):
    pass


class LoginError(NotesError):
    pass


class DeuxFacteursRequis(LoginError):
    """Le SSO demande un code 2FA et on n'a ni clé TOTP ni code fourni à temps."""


@dataclass
class Note:
    cle: str  # identifiant stable : hash(année + chemin complet dans l'arborescence)
    annee: str  # « 4ème année »
    niveau: int  # 0 année … 3 matière, 4 épreuve
    ue: str
    matiere: str  # matière (EC) de rattachement, ou le libellé lui-même au-dessus
    epreuve: str  # « Evaluation Terminale »… vide si la ligne est la matière / l'UE
    valeur: str  # « 13.95/20 », « VALIDE » (période entreprise…), "" = pas encore publiée
    resultat: str = ""  # VALIDE, NON VALIDE, AJ…
    semestre: str = ""
    finale: bool = True  # pas de ligne-fille : épreuve, ou matière sans épreuves

    @property
    def publiee(self) -> bool:
        return bool(self.valeur.strip())

    @property
    def detail(self) -> bool:
        """Matière ou épreuve (les moyennes d'UE / semestre / année ne sont pas annoncées)."""
        return self.niveau >= NIV_MATIERE

    @property
    def libelle(self) -> str:
        return f"{self.matiere} › {self.epreuve}" if self.epreuve else self.matiere

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        noms = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in noms})


def _cle(*parts: str) -> str:
    return hashlib.sha1("|".join(p.strip().lower() for p in parts).encode()).hexdigest()[:16]


def _propre(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


# ── Connexion (SSO Keycloak INSA : mot de passe puis passkey → on bascule sur le TOTP) ──
_OTP_SEL = "input#otp, input[name=otp], input#totp, input[name=totp], input[name=code]:not([type=hidden])"
_ERR_SEL = "#input-error, #input-error-otp-code, .pf-m-error, .alert-error, .kc-feedback-text"

# Callback appelé quand un code 2FA est nécessaire et qu'il n'y a pas de clé TOTP :
# reçoit le texte affiché par la page, renvoie le code (ou None si personne ne répond).
DemandeCode = Callable[[str], Awaitable[str | None]]


async def _erreur_page(page: Page) -> str:
    try:
        return _propre(" ".join(await page.locator(_ERR_SEL).all_inner_texts()))
    except Exception:
        return ""


async def _login(page: Page, username: str, password: str, totp_secret: str = "",
                 demande_code: DemandeCode | None = None) -> None:
    if LOGIN_HOST not in page.url:
        return  # session (cookies sauvegardés) encore valide
    log.info("Session INSA expirée : connexion")
    if await page.locator("#username").count():
        await page.fill("#username", username)
    await page.fill("#password", password)
    await page.click("#kc-login")
    try:
        await page.wait_for_url(lambda u: LOGIN_HOST not in u, timeout=10_000)
        return
    except PWTimeout:
        pass
    # Toujours sur le SSO : erreur d'identifiants, ou étape 2FA. Keycloak propose d'abord la
    # passkey (#authenticateWebAuthnButton) : impossible côté serveur → « Essayer une autre
    # méthode » puis l'application d'authentification (TOTP).
    if not await page.locator(_OTP_SEL).count() and await page.locator("#try-another-way").count():
        await page.click("#try-another-way")
        await page.wait_for_load_state("domcontentloaded")
        choix = page.locator(
            ".select-auth-box-parent, .pf-c-data-list__item, .pf-v5-c-data-list__item, "
            "#kc-select-credential-form button, #kc-select-credential-form a"
        ).filter(has_text=re.compile(r"authentif|OTP|code|application", re.I),
                 has_not_text=re.compile(r"cl[ée] d.acc[eè]s|passkey|webauthn|s[ée]curit[ée]", re.I))
        if await choix.count():
            await choix.first.click()
            await page.wait_for_load_state("domcontentloaded")
    if not await page.locator(_OTP_SEL).count():
        raise LoginError(await _erreur_page(page) or f"Connexion INSA bloquée sur {page.url}")
    consigne = _propre(await page.locator("form").first.inner_text())[:300]
    if totp_secret:
        import pyotp

        code = pyotp.TOTP(totp_secret.replace(" ", "").upper()).now()
    elif demande_code:
        code = await demande_code(consigne)
        if not code:
            raise DeuxFacteursRequis("Code 2FA non fourni à temps")
    else:
        raise DeuxFacteursRequis(f"2FA demandé : {consigne}")
    await page.locator(_OTP_SEL).first.fill(code.strip())
    # « Faire confiance à cet appareil » s'il existe : moins de 2FA ensuite
    for sel in ("input[name=trustDevice]", "input#trustDevice", "input[name=rememberMe]"):
        if await page.locator(sel).count():
            await page.locator(sel).first.check()
    await page.locator("#kc-login, input[type=submit], button[type=submit]").first.click()
    try:
        await page.wait_for_url(lambda u: LOGIN_HOST not in u, timeout=NAV_TIMEOUT)
    except PWTimeout:
        raise LoginError(await _erreur_page(page) or "Code 2FA refusé")


# ── Lecture de l'arborescence des notes ─────────────────────────────────────────────────
_JS_LIGNES = r"""async () => {
  const grid = [...document.querySelectorAll('vaadin-tree-grid, vaadin-grid')]
    .find(g => g.getClientRects().length);
  if (!grid) return null;
  grid.allRowsVisible = true;   // pas de virtualisation : toutes les lignes sont rendues
  for (let pass = 0; pass < 6; pass++) {   // tout déplier (les enfants viennent du serveur)
    const fermes = [...grid.querySelectorAll('vaadin-grid-tree-toggle:not([leaf]):not([expanded])')];
    if (!fermes.length) break;
    fermes.forEach(t => t.click());
    await new Promise(r => setTimeout(r, 1200));
  }
  await new Promise(r => setTimeout(r, 800));
  const rows = [];
  for (const tr of grid.shadowRoot.querySelectorAll('tbody#items tr')) {
    if (tr.hidden) continue;
    const cells = [...tr.querySelectorAll('td')].map(td => {
      const slot = td.querySelector('slot');
      return slot ? slot.assignedElements({flatten: true}) : [];
    });
    const toggle = cells.flat()
      .map(e => e.tagName === 'VAADIN-GRID-TREE-TOGGLE' ? e : e.querySelector('vaadin-grid-tree-toggle'))
      .find(Boolean);
    const txt = cells.map(els => els.map(e => e.innerText).join(' ').trim().replace(/\s+/g, ' '));
    if (!txt.some(Boolean)) continue;
    rows.push({niveau: toggle ? Number(toggle.getAttribute('level') ?? toggle.level ?? 0) : 0, cells: txt});
  }
  return rows;
}"""

_RE_NOTE = re.compile(r"\d{1,2}(?:[.,]\d{1,3})?\s*/\s*20")
_RE_RESULTAT = re.compile(r"\b(NON VALIDE|VALIDE|ADM|AJ|AJAC|ABI|ABJ|DEF)\b")


def _valeur(texte: str) -> tuple[str, str]:
    """« 13.95/20 VALIDE 13.95/20 VALIDE » → ("13.95/20", "VALIDE")."""
    if not texte or "aucun résultat" in texte.lower():
        return "", ""
    note = _RE_NOTE.search(texte)
    res = _RE_RESULTAT.search(texte.upper())
    valeur = note.group(0).replace(" ", "") if note else (res.group(1) if res else _propre(texte)[:40])
    return valeur, (res.group(1) if res and note else "")


def lignes_vers_notes(rows: list[dict]) -> list[Note]:
    notes: list[Note] = []
    chemin: list[str] = []
    for r in rows:
        niveau, cells = int(r["niveau"]), r["cells"]
        libelle = _propre(cells[0]) if cells else ""
        if not libelle:
            continue
        chemin = chemin[:niveau] + [libelle]
        annee = chemin[0]
        ue = chemin[NIV_UE] if len(chemin) > NIV_UE else ""
        if niveau >= NIV_EPREUVE:
            matiere, epreuve = chemin[NIV_MATIERE], libelle
        else:
            matiere, epreuve = libelle, ""
        valeur, resultat = _valeur(" ".join(cells[1:]))
        semestre = chemin[NIV_SEMESTRE] if niveau > NIV_SEMESTRE else ""
        if notes and niveau > notes[-1].niveau:
            notes[-1].finale = False  # la ligne précédente a des enfants
        notes.append(Note(_cle(*chemin), annee, niveau, ue, matiere, epreuve, valeur, resultat, semestre))
    return notes


def resume(notes: list[Note]) -> list[dict]:
    """Vue anonyme des matières/épreuves (sans aucune note) pour les récapitulatifs.
    L'ordre est celui de Mon Dossier Web : la première année est la plus récente."""
    return [
        {"annee": n.annee, "semestre": n.semestre, "matiere": n.matiere, "epreuve": n.epreuve,
         "finale": n.finale, "publiee": n.publiee}
        for n in notes if n.detail
    ]


def compte_par_annee(res: list[dict]) -> dict[str, tuple[int, int]]:
    """{année: (notes publiées, notes attendues)}, en ne comptant que les lignes finales."""
    out: dict[str, tuple[int, int]] = {}
    for r in res:
        if r["finale"]:
            pub, tot = out.get(r["annee"], (0, 0))
            out[r["annee"]] = (pub + r["publiee"], tot + 1)
    return out


def detail_annee(res: list[dict], annee: str) -> dict[str, list[tuple[str, int, int]]]:
    """{semestre: [(matière, notes publiées, notes attendues)]} pour une année."""
    par_sem: dict[str, dict[str, list[int]]] = {}
    for r in res:
        if r["annee"] != annee or not r["finale"]:
            continue
        m = par_sem.setdefault(r["semestre"] or "Année", {}).setdefault(r["matiere"], [0, 0])
        m[0] += r["publiee"]
        m[1] += 1
    return {s: [(mat, p, t) for mat, (p, t) in mats.items()] for s, mats in par_sem.items()}


async def _lire_inscriptions(page: Page) -> list[Note]:
    boutons = page.locator("vaadin-button", has_text="Notes et résultats")
    try:
        await boutons.first.wait_for(timeout=NAV_TIMEOUT)
    except PWTimeout:
        raise NotesError(f"Bouton « Notes et résultats » introuvable sur {page.url}")
    toutes: list[Note] = []
    for i in range(await boutons.count()):
        await boutons.nth(i).click()
        await page.wait_for_selector("vaadin-tree-grid, vaadin-grid", timeout=NAV_TIMEOUT)
        await page.wait_for_timeout(1500)
        rows = await page.evaluate(_JS_LIGNES)
        if not rows:
            raise NotesError(f"Tableau des notes vide (inscription n°{i + 1})")
        toutes += lignes_vers_notes(rows)
        await page.locator("vaadin-button", has_text="Fermer").first.click()
        await page.wait_for_timeout(800)
    return toutes


async def fetch_notes(
    username: str, password: str, url: str = URL_DEFAUT, debug_dir: Path | None = None, *,
    session_file: Path | None = None, totp_secret: str = "", demande_code: DemandeCode | None = None,
) -> list[Note]:
    """Relève toutes les notes (toutes les années d'inscription).

    Les cookies sont gardés dans ``session_file`` : tant que la session SSO de l'INSA est
    valide, ni mot de passe ni 2FA."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            etat = str(session_file) if session_file and session_file.exists() else None
            ctx = await browser.new_context(locale="fr-FR", timezone_id="Europe/Paris",
                                            storage_state=etat, viewport={"width": 1400, "height": 900})
            page = await ctx.new_page()
            page.set_default_timeout(NAV_TIMEOUT)
            await page.goto(url, wait_until="domcontentloaded")
            try:
                await _login(page, username, password, totp_secret, demande_code)
                if "/inscriptions" not in page.url:
                    await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle")
                notes = await _lire_inscriptions(page)
            except Exception:
                if debug_dir is not None:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(debug_dir / "erreur.png"), full_page=True)
                    (debug_dir / "erreur.html").write_text(await page.content(), encoding="utf-8")
                raise
            if session_file:
                await ctx.storage_state(path=str(session_file))
                session_file.chmod(0o600)
            if not notes:
                raise NotesError("Aucune ligne de note lue (page changée ?)")
            return notes
        finally:
            await browser.close()


def _empreinte(valeur: str) -> str:
    return hashlib.sha256(valeur.encode()).hexdigest()[:12]


def instantane(notes: list[Note]) -> dict[str, str]:
    """Ce qui est gardé en base entre deux relevés : clé de ligne → empreinte de la note.
    Les notes elles-mêmes ne sont jamais stockées (ni affichées)."""
    return {n.cle: _empreinte(n.valeur) for n in notes if n.publiee and n.detail}


def diff(avant: dict[str, str], apres: list[Note]) -> list[Note]:
    """Matières / épreuves dont la note est apparue ou a changé depuis le dernier relevé."""
    return [n for n in apres if n.publiee and n.detail and avant.get(n.cle) != _empreinte(n.valeur)]


def matieres(notes: list[Note]) -> list[str]:
    """Matières concernées, sans doublon (pour l'annonce publique, jamais les notes)."""
    vues: dict[str, None] = {}
    for n in notes:
        vues.setdefault(n.matiere, None)
    return list(vues)


if __name__ == "__main__":  # python -m menucrous.notes : relevé manuel (captures dans debug/ si erreur)
    import os

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    async def code_console(consigne: str) -> str:
        print(f"\n2FA demandé : {consigne}")
        return await asyncio.to_thread(input, "Code : ")

    data = Path(os.environ.get("DATA_DIR", "./data"))
    res = asyncio.run(
        fetch_notes(
            os.environ["INSA_USERNAME"], os.environ["INSA_PASSWORD"],
            os.environ.get("NOTES_URL") or URL_DEFAUT,
            data / "debug", session_file=data / "insa-session.json",
            totp_secret=os.environ.get("INSA_TOTP_SECRET", ""), demande_code=code_console,
        )
    )
    publiees = [n for n in res if n.publiee and n.detail]
    print(f"{len(res)} lignes, {len(publiees)} notes publiées (matières/épreuves), valeurs masquées")
    for m in matieres(publiees)[-5:]:
        print(f"  {m}")
