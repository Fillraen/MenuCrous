"""🤫 Easter egg : un mot japonais écrit en rōmaji dans un message → « Baka ! » + meme d'anime.

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

log = logging.getLogger(__name__)

# Mots/expressions courants (fautes de frappe et voyelles étirées tolérées : « sugoiiii »)
MOTS = [
    "tadaima", "okaeri", "okaerinasai", "kakkoii", "kawaii", "sugoi", "sugee", "arigatou", "arigato",
    "arigatou gozaimasu", "konnichiwa", "konbanwa", "ohayo", "ohayou", "oyasumi", "sayonara", "sayounara",
    "itadakimasu", "gochisousama", "nani", "senpai", "sempai", "desu", "yamete", "yamete kudasai", "yamero",
    "onii chan", "oniichan", "onee chan", "oneechan", "nee chan", "daijoubu", "daijobu", "gomen", "gomenasai",
    "gomen nasai", "sumimasen", "moshi moshi", "ikuzo", "omae wa mou shindeiru", "wakarimasen", "wakatta",
    "yatta", "yosh", "yoshi", "ganbatte", "ganbare", "kanpai", "kampai", "oishii", "urusai", "dattebayo",
    "baka", "bakayaro", "nande", "naruhodo", "souka", "sou desu ne", "ara ara", "nyan", "itai", "kimochi",
    "dame", "ikimasu", "mattaku", "sasuga", "uso", "hontou", "honto ni", "chotto matte", "matte",
    "irasshaimase", "kudasai", "doumo", "domo arigato", "ittekimasu", "itterasshai", "otsukare",
    "otsukaresama", "shinzou wo sasageyo", "plus ultra", "nakama", "kami sama", "kamisama",
]
# Mots trop ambigus en français : seulement s'ils sont seuls dans le message
SEULS = {"uso", "dame", "matte", "itai", "nani", "baka", "souka"}

_ETIRE = lambda m: "".join(re.escape(c) + ("+" if c in "aeiou" else "") for c in m)  # noqa: E731


def _motif(mot: str) -> str:
    return r"[\s\-_]*".join(_ETIRE(part) for part in mot.split())


RE_MOTS = re.compile(r"(?<![\w])(" + "|".join(_motif(m) for m in sorted(MOTS, key=len, reverse=True)) + r")(?![\w])",
                     re.IGNORECASE)
RE_SEUL = re.compile(r"^\W*(" + "|".join(_motif(m) for m in SEULS) + r")\W*$", re.IGNORECASE)

REPONSES = [
    "Baka ! 😤", "B-baka ! C'est pas comme si j'avais envie de te répondre... 😳", "Baka baka baka ! 💢",
    "Nani ?! ... Baka. 🙄", "Weeb détecté. Baka ! 🍙", "Hmph. Baka. 😒", "Omae wa mou... baka. 💀",
    "Baka-senpai ! 🙈",
]
SUBREDDITS = ["Animemes", "goodanimemes", "anime_irl"]
COOLDOWN = 45  # secondes par salon
_dernier: dict[int, float] = {}


def detecte(texte: str) -> str | None:
    """Renvoie le mot japonais trouvé (ou None)."""
    if not texte or len(texte) > 1500:
        return None
    texte = re.sub(r"https?://\S+|<[@#:a-z][^>]*>|`[^`]*`", " ", texte)  # liens, mentions, code
    m = RE_MOTS.search(texte)
    if not m:
        return None
    mot = m.group(1).lower()
    base = re.sub(r"(.)\1+", r"\1", re.sub(r"[\s\-_]", "", mot))
    if base in {re.sub(r"(.)\1+", r"\1", s) for s in SEULS} and not RE_SEUL.match(texte):
        return None
    return mot


async def meme() -> tuple[str, str]:
    """(url de l'image, légende). Meme Reddit SFW, sinon GIF « baka » de nekos.best."""
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "MenuCrous/2.0 (bot Discord)"}) as client:
        for _ in range(3):
            try:
                r = await client.get(f"https://meme-api.com/gimme/{random.choice(SUBREDDITS)}")
                d = r.json()
                if (not d.get("nsfw") and not d.get("spoiler")
                        and re.search(r"\.(png|jpe?g|gif|webp)$", d.get("url", ""), re.I)):
                    return d["url"], f"r/{d['subreddit']} · {d.get('title', '')[:200]}"
            except Exception as exc:
                log.debug("meme-api : %s", exc)
        r = await client.get("https://nekos.best/api/v2/baka")
        res = r.json()["results"][0]
        return res["url"], f"{res.get('anime_name', 'anime')}"


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
        url, legende = await meme()
    except Exception as exc:
        log.warning("Easter egg : pas de meme (%s)", exc)
        await message.reply(random.choice(REPONSES), mention_author=False)
        return
    e = discord.Embed(color=0xFF6B9D).set_image(url=url).set_footer(text=f"« {mot} » détecté · {legende}")
    await message.reply(random.choice(REPONSES), embed=e, mention_author=False)
