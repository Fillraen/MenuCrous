"""🤫 Easter egg : un mot japonais écrit en rōmaji dans un message → « Baka ! » + GIF d'anime.

Détection en deux temps :
  1. liste de mots/expressions connus (kawaii, senpai, ara ara…), toujours reconnus ;
  2. sinon, chaque mot du message est découpé en syllabes japonaises (ka, shi, tsu, kyo, n…) :
     s'il se découpe entièrement, a au moins 3 syllabes différentes et n'est PAS un mot
     courant en français / anglais / espagnol / italien (wordfreq), c'est du rōmaji.
     → « tadaima », « yokatta », « hajimemashite » oui ; « tomate », « domaine », « hahaha » non.

GIF : nekos.best (réactions d'anime, SFW uniquement).

Demande l'intent privilégié « Message Content » (Developer Portal → Bot), activé seulement si
SECRET_ROMAJI=1. Volontairement absent de /aide.
"""
from __future__ import annotations

import logging
import random
import re
import time

import discord
import httpx
from wordfreq import zipf_frequency

log = logging.getLogger(__name__)

# ── 1. Mots connus (y compris ceux que le filtre de fréquence écarterait) ────────────────
MOTS = [
    "kawaii", "senpai", "sempai", "sugoi", "konnichiwa", "nakama", "baka", "nani", "desu", "ara ara",
    "onii chan", "oniichan", "onee chan", "oneechan", "nee chan", "yamete kudasai", "moshi moshi",
    "omae wa mou shindeiru", "chotto matte", "sou desu ne", "domo arigato", "arigatou gozaimasu",
    "plus ultra", "shinzou wo sasageyo", "dattebayo", "kamisama", "kami sama", "otsukaresama", "yosh",
]
# Mots qui existent aussi en français : seulement s'ils sont seuls dans le message
SEULS = {"uso", "dame", "matte", "itai", "nani", "baka", "souka", "hai"}


def _motif(mot: str) -> str:
    etire = lambda p: "".join(re.escape(c) + ("+" if c in "aeiou" else "") for c in p)  # noqa: E731
    return r"[\s\-_]*".join(etire(part) for part in mot.split())


RE_MOTS = re.compile(r"(?<![\w])(" + "|".join(_motif(m) for m in sorted(MOTS, key=len, reverse=True))
                     + r")(?![\w])", re.IGNORECASE)
RE_SEUL = re.compile(r"^\W*(" + "|".join(_motif(m) for m in SEULS) + r")\W*$", re.IGNORECASE)

# ── 2. Syllabes du rōmaji (Hepburn) ─────────────────────────────────────────────────────
_SYL = r"(?:ky|gy|sh|ch|ny|hy|by|py|my|ry|ts|j|f|[kgsztdnhbpmryw])?[aeiou]|n(?![aeiouy])|([kstp])(?=\1)"
RE_SYL_MOT = re.compile(r"^(?:" + _SYL + r")+$")
RE_SYL = re.compile(_SYL)
SEUIL_FREQ = 1.5  # zipf : au-dessus, c'est un mot courant d'une langue européenne


def _reduit(mot: str) -> str:
    return re.sub(r"(.)\1{2,}", r"\1", mot)  # « sugoiiii » → « sugoi », « ouiii » → « oui »


def est_romaji(mot: str) -> bool:
    mot = _reduit(mot.lower())
    if len(mot) < 5 or not RE_SYL_MOT.match(mot):
        return False
    syllabes = [m.group(0) for m in RE_SYL.finditer(mot) if m.group(0)[-1] in "aeiou"]
    if len(syllabes) < 3 or len(set(syllabes)) < 3:  # hahaha, nanana, kikoo
        return False
    if sum(c in "aeiou" for c in mot) / len(mot) > 0.7:  # ouaiii
        return False
    return max(zipf_frequency(mot, langue) for langue in ("fr", "en", "es", "it")) < SEUIL_FREQ


def detecte(texte: str) -> str | None:
    """Renvoie le mot japonais trouvé (ou None)."""
    if not texte or len(texte) > 1500:
        return None
    texte = re.sub(r"https?://\S+|<[@#:a-z][^>]*>|`[^`]*`|:\w+:", " ", texte)  # liens, mentions, code, émojis
    m = RE_MOTS.search(texte)
    if m:
        mot = m.group(1).lower()
        base = _reduit(re.sub(r"[\s\-_]", "", mot))
        if base not in SEULS or RE_SEUL.match(texte):
            return mot
    if RE_SEUL.match(texte):
        return RE_SEUL.match(texte).group(1).lower()
    for mot in re.findall(r"[A-Za-z]+", texte):
        if est_romaji(mot):
            return mot.lower()
    return None


# ── Réponse ─────────────────────────────────────────────────────────────────────────────
REPONSES = [
    "Baka ! 😤", "B-baka ! C'est pas comme si j'avais envie de te répondre... 😳", "Baka baka baka ! 💢",
    "Nani ?! ... Baka. 🙄", "Weeb détecté. Baka ! 🍙", "Hmph. Baka. 😒", "Baka-senpai ! 🙈",
]
# Réactions nekos.best (catalogue SFW) : baka plus souvent que les autres
REACTIONS = ["baka"] * 4 + ["pout", "smug", "facepalm", "stare", "nope"]
COOLDOWN = 10  # secondes par salon
_dernier: dict[int, float] = {}


async def gif() -> tuple[str, str]:
    """(url du GIF, nom de l'anime) depuis nekos.best."""
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "MenuCrous/2.0 (bot Discord)"}) as client:
        r = await client.get(f"https://nekos.best/api/v2/{random.choice(REACTIONS)}")
        r.raise_for_status()
        res = r.json()["results"][0]
        return res["url"], res.get("anime_name") or "anime"


async def sur_message(message: discord.Message) -> None:
    if message.author.bot or not message.content:
        return
    mot = detecte(message.content)
    if not mot:
        return
    now = time.monotonic()
    if now - _dernier.get(message.channel.id, 0) < COOLDOWN:
        return
    _dernier[message.channel.id] = now
    try:
        url, anime = await gif()
    except Exception as exc:
        log.warning("Easter egg : pas de GIF (%s)", exc)
        await message.reply(random.choice(REPONSES), mention_author=False)
        return
    e = discord.Embed(color=0xFF6B9D).set_image(url=url).set_footer(text=f"« {mot} » détecté · {anime}")
    await message.reply(random.choice(REPONSES), embed=e, mention_author=False)
