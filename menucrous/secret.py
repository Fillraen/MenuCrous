"""🤫 Easter egg : du japonais dans un message → réponse adaptée + GIF d'anime (SFW).

Détection (dans l'ordre) :
  1. écriture japonaise (hiragana, katakana, kanji) ;
  2. mots / expressions connus, rangés par thème (salut, merci, bouffe, dodo, mignon…) ;
  3. suffixes honorifiques collés à un mot : « Nicolas-kun », « prof-sensei », « Léa-chan » ;
  4. terminaisons verbales typiques : -masu, -mashita, -masen, -mashou, -desu, -nai desu… ;
  5. découpage en syllabes japonaises (ka, shi, tsu, kyo, n…) d'un mot qui n'est PAS un mot
     courant en français / anglais / espagnol / italien (wordfreq) : « yokatta », « hajimemashite ».

Chaque thème a ses réponses et ses GIF (catégories nekos.best choisies, toutes SFW).
Demande l'intent privilégié « Message Content », activé seulement si SECRET_ROMAJI=1.
Volontairement absent de /aide.
"""
from __future__ import annotations

import logging
import random
import re

import discord
import httpx
from wordfreq import zipf_frequency

log = logging.getLogger(__name__)

# ── Thèmes : mots → (GIF nekos.best, réponses) ──────────────────────────────────────────
THEMES: dict[str, dict] = {
    "salut": {
        "mots": ["konnichiwa", "konnichiha", "konbanwa", "ohayo", "ohayou", "ohayo gozaimasu", "ohayou gozaimasu",
                 "moshi moshi", "yahallo", "yaho", "hisashiburi", "ossu", "irasshaimase", "hajimemashite",
                 "yoroshiku", "yoroshiku onegaishimasu", "sayonara", "sayounara", "mata ne", "ja ne", "jaa ne",
                 "mata ashita", "bai bai"],
        "gifs": ["wave", "wave", "smile", "happy"],
        "reponses": ["Konnichiwa ! 👋", "Ohayo~ ☀️", "Yaho ! ✨", "Mata ne ! 👋", "Yoroshiku ne ! 🙇",
                     "Irasshaimase ! 🏮"],
    },
    "maison": {
        "mots": ["tadaima", "okaeri", "okaerinasai", "ittekimasu", "itterasshai"],
        "gifs": ["wave", "happy", "hug", "smile"],
        "reponses": ["Okaeri ! 🏠", "Okaerinasai~ 🍵", "Itterasshai ! 🚪", "Tadaima toi-même ! 🏡"],
    },
    "merci": {
        "mots": ["arigatou", "arigato", "arigatou gozaimasu", "arigato gozaimasu", "domo arigato", "doumo",
                 "domo", "sankyuu", "doitashimashite", "dou itashimashite", "otsukare", "otsukaresama",
                 "otsukaresama desu"],
        "gifs": ["thumbsup", "smile", "nod", "happy"],
        "reponses": ["Dou itashimashite ! 🙏", "Iie iie, c'est rien ! 😊", "Otsukaresama ! 🍵",
                     "De rien, nakama ! 🤝"],
    },
    "pardon": {
        "mots": ["gomen", "gomenasai", "gomen nasai", "gomenne", "sumimasen", "shitsurei shimasu",
                 "moushiwake arimasen"],
        "gifs": ["pat", "pat", "smile", "shrug"],
        "reponses": ["Daijoubu, daijoubu ! 🫶", "Ii yo, ii yo~ 😌", "Pardonné... pour cette fois. 😤", "*pat pat* 🫳"],
    },
    "bouffe": {
        "mots": ["itadakimasu", "gochisousama", "gochisousama deshita", "oishii", "oishi", "umai", "umee",
                 "onaka suita", "harahetta", "bento", "onigiri", "dango", "taiyaki", "mochi", "takoyaki",
                 "okonomiyaki", "yakitori", "gyoza", "udon", "soba", "matcha", "onigiri"],
        "gifs": ["nom", "nom", "happy", "feed"],
        "reponses": ["Itadakimasu ! 🍙", "Oishii~ 🍜", "Gochisousama deshita ! 🙏", "Onaka suita... 🍱",
                     "Le CROUS c'est pas du takoyaki mais bon. 🍛"],
    },
    "dodo": {
        "mots": ["oyasumi", "oyasuminasai", "nemui", "nemuii", "nenne", "ohiru"],
        "gifs": ["sleep", "yawn", "sleep", "cuddle"],
        "reponses": ["Oyasumi~ 🌙", "Oyasuminasai ! 💤", "Nemui... moi aussi. 😴", "Yume de aou ne ! 🌌"],
    },
    "mignon": {
        "mots": ["kawaii", "kawaiii", "kawaisou", "kirei", "suki", "daisuki", "daisuki da yo", "aishiteru",
                 "doki doki", "uwu", "nyan", "nya", "neko", "moe", "chibi"],
        "gifs": ["blush", "blush", "happy", "smile", "wink"],
        "reponses": ["K-kawaii ?! 😳", "Doki doki... 💓", "Nyan~ 🐱", "Tu es kawaii toi-même ! 🌸",
                     "Uwu détecté. 🎀"],
    },
    "classe": {
        "mots": ["kakkoii", "sugoi", "sugee", "sugoii", "subarashii", "yabai", "yabee", "sasuga", "saikou",
                 "kanpeki", "omoshiroi", "tanoshii", "ii ne", "nice desu"],
        "gifs": ["thumbsup", "smug", "dance", "happy"],
        "reponses": ["Sugoi desu ne ! ✨", "Yabai yabai ! 🔥", "Sasuga ! 😎", "Kakkoii~ 🗡️", "Saikou ! 🏆"],
    },
    "motivation": {
        "mots": ["ganbatte", "ganbare", "ganbarimasu", "ganbarou", "faito", "ikuzo", "ikou", "yosh", "yoshi",
                 "yatta", "yattaa", "kanpai", "kampai", "plus ultra", "shinzou wo sasageyo", "dattebayo",
                 "nakama", "makenai"],
        "gifs": ["thumbsup", "dance", "highfive", "happy"],
        "reponses": ["Ganbatte ! 💪", "Faito ! 🔥", "Yatta ! 🎉", "Kanpai ! 🍻", "Ikuzo ! ⚔️",
                     "Plus Ultra ! 💥", "Shinzou wo sasageyo ! 🫡"],
    },
    "surprise": {
        "mots": ["nani", "nande", "naze", "eeeh", "ehhh", "uso", "maji", "majide", "honto", "hontou",
                 "hontou ni", "masaka", "naruhodo", "souka", "sou desu ka", "sou desu ne", "wakaranai",
                 "wakarimasen", "shiranai", "chotto matte", "matte", "ara ara", "ara"],
        "gifs": ["stare", "think", "shrug", "facepalm"],
        "reponses": ["NANI ?! 😱", "Masaka... 😶", "Naruhodo naruhodo. 🤔", "Ara ara~ 😏", "Eeeeh ?! 😳",
                     "Maji de ?! 👀"],
    },
    "taquin": {
        "mots": ["baka", "bakayaro", "aho", "urusai", "urusee", "yamete", "yamete kudasai", "yamero", "dame",
                 "dame dame", "itai", "kimoi", "uzai", "omae wa mou shindeiru", "senpai", "sempai",
                 "notice me senpai", "onii chan", "oniichan", "onee chan", "oneechan", "nee chan", "imouto",
                 "otaku", "weeb", "waifu", "husbando", "tsundere", "yandere", "kuudere", "desu", "desu ne",
                 "kudasai", "onegai", "onegaishimasu", "kamisama", "kami sama", "sensei", "kohai"],
        "gifs": ["baka", "baka", "smug", "pout", "nope", "facepalm"],
        "reponses": ["Baka ! 😤", "B-baka ! C'est pas comme si j'avais envie de te répondre... 😳",
                     "Hmph. Baka. 😒", "Urusai urusai urusai ! 💢", "Omae wa mou... baka. 💀",
                     "Senpai m'a remarqué ?! 🙈", "Dame dame ! 🙅", "Weeb détecté. 🍙"],
    },
}
# Thème par défaut (mot japonais trouvé par syllabes, suffixe, terminaison ou écriture japonaise)
DEFAUT = {
    "gifs": ["baka", "smug", "stare", "think", "happy", "wave", "thumbsup", "shrug", "blush", "pout"],
    "reponses": ["Nihongo jouzu desu ne ! 🇯🇵", "Oh, du japonais ! 👀", "Weeb détecté. 🍙", "Sugoi, tu parles japonais ? ✨",
                 "Baka ! 😤", "Naruhodo... 🤔", "Hmph, kawaii. 😳", "Wakarimasen... mais respect. 🙇"],
}

# Mots qui existent aussi en français / anglais : seulement s'ils sont seuls dans le message
SEULS = {"uso", "dame", "matte", "itai", "nani", "baka", "souka", "hai", "ara", "domo", "aho", "moe", "nya",
         "neko", "mochi", "udon", "soba", "ii ne", "sensei", "otaku", "ikou", "honto", "maji", "suki", "yaho",
         "chibi", "weeb", "waifu", "desu", "kirei"}


def _motif(mot: str) -> str:
    etire = lambda p: "".join(re.escape(c) + ("+" if c in "aeiou" else "") for c in p)  # noqa: E731
    return r"[\s\-_]*".join(etire(part) for part in mot.split())


def _norme(mot: str) -> str:
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[\s\-_]", "", mot.lower()))


_MOT_THEME: dict[str, str] = {}
for _theme, _d in THEMES.items():
    for _m in _d["mots"]:
        _MOT_THEME.setdefault(_norme(_m), _theme)
_TOUS = sorted({m for d in THEMES.values() for m in d["mots"]}, key=len, reverse=True)
RE_MOTS = re.compile(r"(?<![\w])(" + "|".join(_motif(m) for m in _TOUS) + r")(?![\w])", re.IGNORECASE)
RE_SEUL = re.compile(r"^\W*(" + "|".join(_motif(m) for m in SEULS) + r")\W*$", re.IGNORECASE)
_SEULS_NORMES = {_norme(m) for m in SEULS}

# ── Autres façons de reconnaître du japonais ────────────────────────────────────────────
RE_ECRITURE = re.compile(r"[぀-ゟ゠-ヿ一-龯ｦ-ﾟ]+")  # kana, kanji
RE_HONORIFIQUE = re.compile(r"(?<![\w])[A-Za-zÀ-ÿ]{2,}-(kun|chan|sama|san|senpai|sensei|dono|tan|chin)(?![\w])",
                            re.IGNORECASE)
RE_TERMINAISON = re.compile(
    r"(?<![\w])[a-z]{2,}(masu|mashita|masen|mashou|mashita|masendeshita|nasai|kudasai|tai desu|desu ka)(?![\w])",
    re.IGNORECASE)
_SYL = r"(?:ky|gy|sh|ch|ny|hy|by|py|my|ry|ts|j|f|[kgsztdnhbpmryw])?[aeiou]|n(?![aeiouy])|([kstp])(?=\1)"
RE_SYL_MOT = re.compile(r"^(?:" + _SYL + r")+$")
RE_SYL = re.compile(_SYL)


def est_romaji(mot: str) -> bool:
    """Mot qui se découpe entièrement en syllabes japonaises et n'est pas un mot européen courant."""
    mot = re.sub(r"(.)\1{2,}", r"\1", mot.lower())
    if len(mot) < 5 or not RE_SYL_MOT.match(mot):
        return False
    syllabes = [m.group(0) for m in RE_SYL.finditer(mot) if m.group(0)[-1] in "aeiou"]
    if len(syllabes) < 3 or len(set(syllabes)) < 3:  # hahaha, nanana, kikoo
        return False
    if sum(c in "aeiou" for c in mot) / len(mot) > 0.7:  # ouaiii
        return False
    return max(zipf_frequency(mot, langue) for langue in ("fr", "en", "es", "it")) < 1.5


def detecte(texte: str) -> tuple[str, str] | None:
    """(mot trouvé, thème) ou None."""
    if not texte or len(texte) > 1500:
        return None
    texte = re.sub(r"https?://\S+|<[@#:a-z][^>]*>|`[^`]*`|:\w+:", " ", texte)  # liens, mentions, code, émojis
    seul = RE_SEUL.match(texte)
    if seul:
        mot = seul.group(1).lower()
        return mot, _MOT_THEME.get(_norme(mot), "taquin")
    for m in RE_MOTS.finditer(texte):
        mot = m.group(1).lower()
        if _norme(mot) not in _SEULS_NORMES:
            return mot, _MOT_THEME.get(_norme(mot), "taquin")
    if (m := RE_ECRITURE.search(texte)):
        return m.group(0), "defaut"
    if (m := RE_HONORIFIQUE.search(texte)):
        return m.group(0).lower(), "taquin"
    if (m := RE_TERMINAISON.search(texte)):
        return m.group(0).lower(), "defaut"
    for mot in re.findall(r"[A-Za-z]+", texte):
        if est_romaji(mot):
            return mot.lower(), "defaut"
    return None


async def gif(categorie: str) -> tuple[str, str]:
    """(url du GIF, nom de l'anime) depuis nekos.best (catégories SFW choisies ci-dessus)."""
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "MenuCrous/2.0 (bot Discord)"}) as client:
        r = await client.get(f"https://nekos.best/api/v2/{categorie}")
        r.raise_for_status()
        res = r.json()["results"][0]
        return res["url"], res.get("anime_name") or "anime"


async def sur_message(message: discord.Message) -> None:
    if message.author.bot or not message.content:
        return
    trouve = detecte(message.content)
    if not trouve:
        return
    mot, theme = trouve
    d = THEMES.get(theme, DEFAUT)
    texte = random.choice(d["reponses"])
    try:
        url, anime = await gif(random.choice(d["gifs"]))
    except Exception as exc:
        log.warning("Easter egg : pas de GIF (%s)", exc)
        await message.reply(texte, mention_author=False)
        return
    e = discord.Embed(color=0xFF6B9D).set_image(url=url).set_footer(text=f"« {mot} » · {anime}")
    await message.reply(texte, embed=e, mention_author=False)
