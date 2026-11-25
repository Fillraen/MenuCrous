"""Client SoWeSign (portail étudiant app.sowesign.com) : signatures à faire et signature.

Reproduit ce que fait l'appli web officielle (Angular, /student) :
  - connexion : POST /api/portal/authentication/token, en-tête « JBAuth base64(code) »,
    code = établissement (4) + identifiant (8) + PIN (4) ; le 5e chiffre « 1 » = étudiant ;
  - à signer : GET /api/student-portal/students/{id}/presenttokens (jeton de présence posé
    par le formateur) et …/recoveries (rattrapages) ;
  - signature : POST …/courses/{id}/signatures (jeton, ou séance en cours après
    POST …/courses/{id}/checkcode) et POST …/recoveries/{id}/signatures. Le corps est
    chiffré comme le fait l'appli (AES « CryptoJS », phrase de passe = constante du code
    de l'appli + identifiants), envoyé en text/plain.

Les constantes de chiffrement sont relues dans le JavaScript public de l'appli (pas copiées
ici) : si SoWeSign les change, le bot suit.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import TZ

log = logging.getLogger(__name__)

BASE = "https://app.sowesign.com"
API = f"{BASE}/api"
SWS = f"{API}/student-portal"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class SwsErreur(RuntimeError):
    pass


class IdentifiantsRefuses(SwsErreur):
    pass


# ── Chiffrement compatible CryptoJS.AES.encrypt(texte, phrase) ──────────────────────────
def _evp_bytes_to_key(phrase: bytes, sel: bytes, n: int = 48) -> bytes:
    d, bloc = b"", b""
    while len(d) < n:
        bloc = hashlib.md5(bloc + phrase + sel).digest()
        d += bloc
    return d[:n]


def cryptojs_encrypt(texte: str, phrase: str) -> str:
    sel = os.urandom(8)
    kiv = _evp_bytes_to_key(phrase.encode(), sel)
    pad = padding.PKCS7(128).padder()
    donnees = pad.update(texte.encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(kiv[:32]), modes.CBC(kiv[32:])).encryptor()
    return base64.b64encode(b"Salted__" + sel + enc.update(donnees) + enc.finalize()).decode()


def cryptojs_decrypt(chiffre: str, phrase: str) -> str:  # pour les tests
    brut = base64.b64decode(chiffre)
    sel, ct = brut[8:16], brut[16:]
    kiv = _evp_bytes_to_key(phrase.encode(), sel)
    dec = Cipher(algorithms.AES(kiv[:32]), modes.CBC(kiv[32:])).decryptor()
    unpad = padding.PKCS7(128).unpadder()
    return (unpad.update(dec.update(ct) + dec.finalize()) + unpad.finalize()).decode()


_cles: tuple[float, dict[str, str]] | None = None


async def cles_appli(client: httpx.AsyncClient) -> dict[str, str]:
    """Constantes {checkCode, signature, documentToSign} lues dans le bundle de l'appli (cache 12 h)."""
    global _cles
    if _cles and time.monotonic() - _cles[0] < 12 * 3600:
        return _cles[1]
    html = (await client.get(f"{BASE}/student/recoveries")).text
    main = re.search(r'src="(main\.[0-9a-f]+\.js)"', html)
    if not main:
        raise SwsErreur("Appli SoWeSign : bundle introuvable (site modifié ?)")
    js = (await client.get(f"{BASE}/student/{main.group(1)}")).text
    bloc = re.search(r"\{checkCode:\"([^\"]+)\",signature:\"([^\"]+)\",documentToSign:\"([^\"]+)\"\}", js)
    if not bloc:
        raise SwsErreur("Appli SoWeSign : constantes de chiffrement introuvables (site modifié ?)")
    cles = dict(zip(("checkCode", "signature", "documentToSign"), bloc.groups()))
    _cles = (time.monotonic(), cles)
    return cles


def _phrase(cles: dict[str, str], type_: str, cours_id: int, user_id: int) -> str:
    r = cles[type_]
    if type_ == "checkCode":
        return f"{r}{cours_id}{user_id}"
    if type_ == "signature":
        return f"{r}{user_id}{cours_id}"
    return f"{user_id}{cours_id}{r}"  # documentToSign


# ── Dates au format de l'appli ──────────────────────────────────────────────────────────
def _decalage(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    m = int(off.total_seconds() // 60)
    return "+00:00" if m == 0 else f"{'+' if m > 0 else '-'}{abs(m) // 60:02d}:{abs(m) % 60:02d}"


def horodatage(dt: datetime | None = None) -> str:
    """« 2026-11-25T21:40:05+01:00 » (fonction zl de l'appli, heure locale de Paris)."""
    dt = (dt or datetime.now(TZ)).astimezone(TZ)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + _decalage(dt)


# ── Modèle ──────────────────────────────────────────────────────────────────────────────
@dataclass
class ASigner:
    """Une signature en attente : jeton de présence (« jeton ») ou rattrapage (« rattrapage »)."""
    nature: str
    id: int  # id du cours (jeton) ou du rattrapage
    cours_id: int
    nom: str
    debut: datetime
    fin: datetime
    brut: dict
    document: bool = False  # document à signer en plus : non géré, à faire dans l'appli

    @property
    def cle(self) -> str:
        return f"{self.nature}:{self.id}"


def _cours_dates(c: dict) -> tuple[datetime, datetime]:
    d = c.get("date", "")[:10]
    deb = datetime.fromisoformat(f"{d}T{c.get('start', '00:00:00')}").replace(tzinfo=TZ)
    fin = datetime.fromisoformat(f"{d}T{c.get('end', '00:00:00')}").replace(tzinfo=TZ)
    return deb, fin


def _a_document(c: dict) -> bool:
    url = c.get("documentToSignUrlValidate") or ""
    return bool(re.match(r"^https://storage(?:-[\w-]+)?\.sowe(?:sign|soft)\.com/", url))


# ── Client ──────────────────────────────────────────────────────────────────────────────
class SwsClient:
    def __init__(self, code: str):
        code = re.sub(r"\D", "", code)
        if len(code) != 16:
            raise IdentifiantsRefuses("Le code doit faire 16 chiffres (établissement 4 + identifiant 8 + PIN 4)")
        if code[4] != "1":
            raise IdentifiantsRefuses("Ce code n'est pas un code étudiant (5e chiffre ≠ 1)")
        self.code = code
        self.jeton: str | None = None
        self.expire = 0.0
        self.utilisateur: dict = {}
        self.http = httpx.AsyncClient(timeout=30, headers={"User-Agent": UA, "Accept": "application/json"})

    async def fermer(self) -> None:
        await self.http.aclose()

    async def connexion(self) -> None:
        r = await self.http.post(
            f"{API}/portal/authentication/token", content="null",
            headers={"Authorization": "JBAuth " + base64.b64encode(self.code.encode()).decode(),
                     "X-Origin": "student", "Content-Type": "application/json"},
        )
        if r.status_code in (400, 401, 403, 404):
            raise IdentifiantsRefuses("Identifiants SoWeSign refusés (établissement / identifiant / PIN)")
        r.raise_for_status()
        corps = r.json()
        self.jeton = corps["token"]
        charge = json.loads(base64.urlsafe_b64decode(self.jeton.split(".")[1] + "=="))
        self.expire = float(charge.get("exp", time.time() + 3000)) - 120
        ent = charge.get("entity") or {}
        self.utilisateur = {"id": ent.get("id"), "firstName": ent.get("firstName", ""),
                            "lastName": ent.get("lastName", "")}
        infos = await self._get(f"/students/{self.utilisateur['id']}"
                                "?include=id,firstName,lastName,requiredSignature")
        if isinstance(infos, dict):
            self.utilisateur.update({k: v for k, v in infos.items() if v is not None})

    async def _entetes(self) -> dict:
        if not self.jeton or time.time() > self.expire:
            await self.connexion()
        return {"Authorization": f"Bearer {self.jeton}"}

    async def _get(self, chemin: str):
        r = await self.http.get(SWS + chemin, headers=await self._entetes())
        if r.status_code == 409:  # « rien à afficher » pour certaines listes
            return []
        r.raise_for_status()
        return r.json() if r.content else None

    async def _post_chiffre(self, chemin: str, charge: dict, type_: str, cours_id: int) -> httpx.Response:
        cles = await cles_appli(self.http)
        corps = cryptojs_encrypt(json.dumps(charge, separators=(",", ":"), ensure_ascii=False),
                                 _phrase(cles, type_, cours_id, self.utilisateur["id"]))
        r = await self.http.post(SWS + chemin, content=corps,
                                 headers={**await self._entetes(), "Content-Type": "text/plain"})
        if r.status_code >= 400:
            raise SwsErreur(f"SoWeSign a refusé ({r.status_code}) : {r.text[:200]}")
        return r

    @property
    def nom_complet(self) -> str:
        return f"{self.utilisateur.get('lastName', '')} {self.utilisateur.get('firstName', '')}".strip()

    @property
    def signature_requise(self) -> bool:
        return bool(self.utilisateur.get("requiredSignature", True))

    # ── Lecture ─────────────────────────────────────────────────────────────
    async def a_signer(self) -> list[ASigner]:
        await self._entetes()
        sid = self.utilisateur["id"]
        out: list[ASigner] = []
        for c in await self._get(f"/students/{sid}/presenttokens?limit=50") or []:
            deb, fin = _cours_dates(c)
            out.append(ASigner("jeton", c["id"], c["id"], c.get("name", "Cours"), deb, fin, c, _a_document(c)))
        for r in await self._get(f"/students/{sid}/recoveries?limit=50") or []:
            c = r.get("course") or {}
            deb, fin = _cours_dates(c) if c.get("date") else (datetime.now(TZ), datetime.now(TZ))
            out.append(ASigner("rattrapage", r["id"], c.get("id", 0), c.get("name", "Cours"), deb, fin, r,
                               _a_document(c)))
        return sorted(out, key=lambda a: a.debut)

    async def cours_ouvert(self) -> dict | None:
        """Séance ouverte à la signature en ce moment (fenêtre d'ouverture de l'appli)."""
        maintenant = datetime.now(TZ)
        for c in await self._get("/future-courses?limit=12") or []:
            deb, fin = _cours_dates(c)
            ouverture = deb - timedelta(minutes=c.get("openingDelay") or 0)
            fermeture = deb + timedelta(minutes=c.get("closingDelay") or 0) if c.get("closingDelay") else fin
            if ouverture <= maintenant <= max(fermeture, fin):
                return c
        return None

    # ── Signature ───────────────────────────────────────────────────────────
    def _charge_signature(self, cours: dict, png: str | None, rattrapage: bool) -> dict:
        present = png is not None
        charge = {
            "signedOn": horodatage(), "collectedOn": horodatage(),
            "md5": hashlib.md5(png.encode()).hexdigest() if present else None,
            "status": "present" if present else "present-token",
            "collectMode": "studentPortal", "signer": self.utilisateur["id"], "course": cours["id"],
            "place": (cours.get("place") or {}).get("id"), "campus": None, "file": png,
        }
        if rattrapage:  # jeton posé par le formateur, séance terminée
            pose = datetime.fromisoformat(cours["signature"]["collectedOn"]).astimezone(TZ)
            charge.update({
                "recovery": True, "recoveryBy": self.nom_complet,
                "recoveryReason": f"Jeton de présence posé par le formateur le {pose:%d/%m/%Y} à {pose:%H:%M:%S}",
                "recoveryRole": "Student",
            })
        return charge

    async def signer(self, item: ASigner, png: str) -> None:
        """png = data URL « data:image/png;base64,… » (la signature dessinée par l'étudiant)."""
        if item.document:
            raise SwsErreur("Ce cours demande aussi de signer un document : à faire dans l'appli SoWeSign")
        if item.nature == "rattrapage":
            charge = {"file": png, "addedBy": self.nom_complet, "addedRole": "Signataire", "signedOn": horodatage()}
            r = await self.http.post(f"{SWS}/recoveries/{item.id}/signatures", json=charge,
                                     headers=await self._entetes())
            if r.status_code >= 400:
                raise SwsErreur(f"SoWeSign a refusé ({r.status_code}) : {r.text[:200]}")
            return
        cours = item.brut
        termine = item.fin < datetime.now(TZ)
        await self._post_chiffre(f"/courses/{cours['id']}/signatures",
                                 self._charge_signature(cours, png if self.signature_requise else None, termine),
                                 "signature", cours["id"])

    async def signer_avec_code(self, code_seance: str, png: str) -> dict:
        """Séance en cours : vérifie le code donné par le formateur, puis signe. Renvoie le cours."""
        cours = await self.cours_ouvert()
        if not cours:
            raise SwsErreur("Aucune séance ouverte à la signature en ce moment")
        if _a_document(cours):
            raise SwsErreur("Cette séance demande aussi de signer un document : à faire dans l'appli SoWeSign")
        await self._post_chiffre(f"/courses/{cours['id']}/checkcode",
                                 {"value": code_seance.strip(), "type": 2}, "checkCode", cours["id"])
        await self._post_chiffre(f"/courses/{cours['id']}/signatures",
                                 self._charge_signature(cours, png if self.signature_requise else None, False),
                                 "signature", cours["id"])
        return cours
