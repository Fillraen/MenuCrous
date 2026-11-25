"""SoWeSign côté Discord : inscription, signatures, notifications, boutons « Signer ».

Règle : chacun ne signe que pour soi. Toute action (bouton, commande) utilise le compte de la
personne qui clique / tape la commande, jamais celui de quelqu'un d'autre.
"""
from __future__ import annotations

import io
import logging
import secrets
import time
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from aiohttp import web
from discord import app_commands

from . import signature as sig
from . import sws as sws_mod
from .config import TZ
from .db import SwsCompte
from .format import JOURS

if TYPE_CHECKING:
    from .bot import MenuCrous

log = logging.getLogger(__name__)
COULEUR = 0x00A3E0
APPLI = "https://app.sowesign.com/student/recoveries"
JETON_TTL = 15 * 60


def _bot(inter: discord.Interaction) -> "MenuCrous":
    return inter.client  # type: ignore[return-value]


MOIS_COURTS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _h(dt: datetime) -> str:
    return f"{dt.hour}h{dt.minute:02d}" if dt.minute else f"{dt.hour}h"


def _quand(a: sws_mod.ASigner) -> str:
    d = a.debut
    return f"{JOURS[d.weekday()][:3]}. {d.day} {MOIS_COURTS[d.month - 1]} {_h(d)}–{_h(a.fin)}"


def _ligne(a: sws_mod.ASigner) -> str:
    nature = "jeton du formateur" if a.nature == "jeton" else "rattrapage"
    doc = " · 📄 document à signer dans l'appli" if a.document else ""
    return f"📝 **{a.nom}** — {_quand(a)} *({nature})*{doc}"


def embed_attente(items: list[sws_mod.ASigner], titre: str = "") -> discord.Embed:
    if not items:
        return discord.Embed(title="✅ Rien à signer sur SoWeSign", color=0x2ECC71)
    e = discord.Embed(title=titre or f"✍️ {len(items)} signature{'s' if len(items) > 1 else ''} à faire",
                      color=COULEUR, url=APPLI, description="\n".join(_ligne(a) for a in items[:20]))
    return e.set_footer(text="« Signer » utilise TON compte et TA signature enregistrée")


class BoutonsSignature(discord.ui.View):
    """Boutons sous la liste. Les custom_id sont traités par MenuCrous.on_interaction (survit aux
    redémarrages) : la vue ne sert qu'à les afficher."""

    def __init__(self, items: list[sws_mod.ASigner]):
        super().__init__(timeout=None)
        signables = [a for a in items if not a.document]
        for a in signables[:4]:
            self.add_item(discord.ui.Button(label=f"Signer : {a.nom[:40]}", style=discord.ButtonStyle.primary,
                                            emoji="✍️", custom_id=f"sws:signer:{a.cle}"))
        if len(signables) > 1:
            self.add_item(discord.ui.Button(label=f"Tout signer ({len(signables)})", emoji="✅",
                                            style=discord.ButtonStyle.success, custom_id="sws:tout"))
        self.add_item(discord.ui.Button(label="Ouvrir SoWeSign", url=APPLI))


# ── Accès au compte de l'utilisateur ────────────────────────────────────────────────────
async def client_de(bot: "MenuCrous", user_id: int) -> tuple[SwsCompte, sws_mod.SwsClient] | None:
    compte = await bot.db.sws_compte(user_id)
    if not compte:
        return None
    cli = bot.sws_clients.get(user_id)
    if cli is None or cli.code != compte.code:
        cli = sws_mod.SwsClient(compte.code)
        bot.sws_clients[user_id] = cli
    return compte, cli


async def _exiger_compte(inter: discord.Interaction):
    res = await client_de(_bot(inter), inter.user.id)
    if res is None:
        msg = "Tu n'es pas inscrit : `/sws inscription` (tes codes SoWeSign, en privé)."
        if inter.response.is_done():
            await inter.followup.send(msg, ephemeral=True)
        else:
            await inter.response.send_message(msg, ephemeral=True)
    return res


async def signer_items(bot: "MenuCrous", compte: SwsCompte, cli: sws_mod.SwsClient,
                       items: list[sws_mod.ASigner]) -> tuple[list[str], list[str]]:
    ok, ko = [], []
    for a in items:
        try:
            await cli.signer(a, sig.au_hasard(compte.signatures))
            ok.append(f"✅ {a.nom} — {_quand(a)}")
        except Exception as exc:
            ko.append(f"❌ {a.nom} — {_quand(a)} : {str(exc)[:150]}")
            log.warning("Signature SoWeSign (%s, %s) : %s", compte.user_id, a.cle, exc)
    return ok, ko


# ── Boutons (appelé par MenuCrous.on_interaction) ───────────────────────────────────────
async def gerer_bouton(inter: discord.Interaction) -> None:
    cid = (inter.data or {}).get("custom_id", "")
    await inter.response.defer(ephemeral=True, thinking=True)
    res = await _exiger_compte(inter)
    if res is None:
        return
    compte, cli = res
    if not compte.signatures:
        await inter.followup.send("Enregistre d'abord ta signature : `/sws signature`.", ephemeral=True)
        return
    try:
        attente = await cli.a_signer()  # toujours relu : on ne signe que ce qui est vraiment en attente
    except Exception as exc:
        await inter.followup.send(f"❌ SoWeSign injoignable : {exc}", ephemeral=True)
        return
    if cid == "sws:tout":
        cibles = [a for a in attente if not a.document]
    else:
        cle = cid.removeprefix("sws:signer:")
        cibles = [a for a in attente if a.cle == cle]
        if not cibles:
            await inter.followup.send("Cette signature n'est plus en attente sur ton compte (déjà faite ?).",
                                      ephemeral=True)
            return
    if not cibles:
        await inter.followup.send("✅ Rien à signer.", ephemeral=True)
        return
    ok, ko = await signer_items(_bot(inter), compte, cli, cibles)
    await inter.followup.send("\n".join(ok + ko)[:1900], ephemeral=True)


# ── Commandes /sws ──────────────────────────────────────────────────────────────────────
sws_group = app_commands.Group(
    name="sws", description="SoWeSign : signatures à faire, rappels, signature",
    allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
)


class Inscription(discord.ui.Modal, title="Connexion SoWeSign"):
    etab = discord.ui.TextInput(label="Établissement (4 chiffres)", min_length=4, max_length=4, placeholder="0377")
    ident = discord.ui.TextInput(label="Code identifiant (8 chiffres)", min_length=8, max_length=9,
                                 placeholder="1234 5678")
    pin = discord.ui.TextInput(label="Code PIN (4 chiffres)", min_length=4, max_length=4)

    async def on_submit(self, inter: discord.Interaction) -> None:
        await inter.response.defer(ephemeral=True, thinking=True)
        code = "".join(c for c in f"{self.etab.value}{self.ident.value}{self.pin.value}" if c.isdigit())
        try:
            cli = sws_mod.SwsClient(code)
            await cli.connexion()
        except sws_mod.IdentifiantsRefuses as exc:
            await inter.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            await inter.followup.send(f"❌ SoWeSign injoignable : {exc}", ephemeral=True)
            return
        bot = _bot(inter)
        ancien = await bot.db.sws_compte(inter.user.id)
        compte = SwsCompte(inter.user.id, code, cli.nom_complet, ancien.signatures if ancien else [], True,
                           ancien.connus if ancien else [], None, datetime.now(TZ).isoformat())
        await bot.db.set_sws_compte(compte)
        bot.sws_clients[inter.user.id] = cli
        suite = ("Ta signature est déjà enregistrée." if compte.signatures else
                 "Étape suivante : **`/sws signature`** pour enregistrer TA signature.")
        await inter.followup.send(
            f"✅ Connecté à SoWeSign en tant que **{cli.nom_complet}**. Codes stockés chiffrés.\n{suite}\n"
            "Tu recevras un MP quand une signature est à faire (`/sws notifications` pour couper).",
            ephemeral=True)


@sws_group.command(name="inscription", description="Enregistrer TES codes SoWeSign (fenêtre privée)")
async def sws_inscription(inter: discord.Interaction) -> None:
    if not _bot(inter).settings.sws_secret:
        await inter.response.send_message("SoWeSign n'est pas activé sur ce bot (SWS_SECRET).", ephemeral=True)
        return
    await inter.response.send_modal(Inscription())


@sws_group.command(name="signature", description="Enregistrer ta signature (dessin sur une page web, ou photo)")
@app_commands.describe(photo="Photo de ta signature (stylo foncé sur papier blanc). Sans photo : lien pour dessiner")
async def sws_signature(inter: discord.Interaction, photo: discord.Attachment | None = None) -> None:
    bot = _bot(inter)
    res = await _exiger_compte(inter)
    if res is None:
        return
    compte, _ = res
    if photo is None:
        if not bot.settings.public_url:
            await inter.response.send_message("Envoie une photo : `/sws signature photo:`.", ephemeral=True)
            return
        jeton = secrets.token_urlsafe(24)
        bot.sws_jetons[jeton] = (inter.user.id, time.time() + JETON_TTL)
        await inter.response.send_message(
            f"✍️ Dessine ta signature ici (lien personnel, 15 min) :\n{bot.settings.public_url}/sws/signature/{jeton}\n"
            f"Tu peux en enregistrer jusqu'à {sig.MAX_SIGNATURES} : le bot en prend une au hasard à chaque fois.",
            ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    if photo.size > 10_000_000:
        await inter.followup.send("❌ Image trop lourde (10 Mo max).", ephemeral=True)
        return
    try:
        png = sig.depuis_image(await photo.read())
    except sig.SignatureInvalide as exc:
        await inter.followup.send(f"❌ {exc}", ephemeral=True)
        return
    await _ajouter_signature(bot, compte, png)
    await inter.followup.send(
        f"✅ Signature enregistrée ({len(compte.signatures)}/{sig.MAX_SIGNATURES}). Voilà ce qui sera envoyé :",
        file=discord.File(io.BytesIO(sig.apercu(png)), "signature.png"), ephemeral=True)


async def _ajouter_signature(bot: "MenuCrous", compte: SwsCompte, png: str) -> None:
    compte.signatures = (compte.signatures + [png])[-sig.MAX_SIGNATURES:]
    await bot.db.set_sws_compte(compte)


@sws_group.command(name="signatures-effacer", description="Supprimer toutes tes signatures enregistrées")
async def sws_signatures_effacer(inter: discord.Interaction) -> None:
    res = await _exiger_compte(inter)
    if res is None:
        return
    compte, _ = res
    compte.signatures = []
    await _bot(inter).db.set_sws_compte(compte)
    await inter.response.send_message("🗑️ Signatures supprimées. `/sws signature` pour en refaire.", ephemeral=True)


@sws_group.command(name="attente", description="Mes signatures à faire (jetons du formateur, rattrapages)")
async def sws_attente(inter: discord.Interaction) -> None:
    await inter.response.defer(ephemeral=True, thinking=True)
    res = await _exiger_compte(inter)
    if res is None:
        return
    _, cli = res
    try:
        items = await cli.a_signer()
    except Exception as exc:
        await inter.followup.send(f"❌ SoWeSign injoignable : {exc}", ephemeral=True)
        return
    await inter.followup.send(embed=embed_attente(items), view=BoutonsSignature(items) if items else discord.utils.MISSING,
                              ephemeral=True)


@sws_group.command(name="code", description="Signer la séance en cours avec le code donné par le formateur")
@app_commands.describe(code="Code de la séance")
async def sws_code(inter: discord.Interaction, code: str) -> None:
    await inter.response.defer(ephemeral=True, thinking=True)
    res = await _exiger_compte(inter)
    if res is None:
        return
    compte, cli = res
    if not compte.signatures:
        await inter.followup.send("Enregistre d'abord ta signature : `/sws signature`.", ephemeral=True)
        return
    try:
        cours = await cli.signer_avec_code(code, sig.au_hasard(compte.signatures))
    except Exception as exc:
        await inter.followup.send(f"❌ {str(exc)[:300]}", ephemeral=True)
        return
    await inter.followup.send(f"✅ Signé : **{cours.get('name', 'séance')}** ({cours.get('start', '')[:5]}–"
                              f"{cours.get('end', '')[:5]}).", ephemeral=True)


@sws_group.command(name="notifications", description="Recevoir (ou non) un MP quand une signature est à faire")
async def sws_notifications(inter: discord.Interaction, actif: bool) -> None:
    res = await _exiger_compte(inter)
    if res is None:
        return
    compte, _ = res
    compte.notif = actif
    await _bot(inter).db.set_sws_compte(compte)
    await inter.response.send_message("🔔 Notifications activées." if actif else "🔕 Notifications coupées.",
                                      ephemeral=True)


@sws_group.command(name="statut", description="Mon compte SoWeSign sur le bot")
async def sws_statut(inter: discord.Interaction) -> None:
    res = await _exiger_compte(inter)
    if res is None:
        return
    compte, _ = res
    e = discord.Embed(title="✍️ SoWeSign", color=COULEUR)
    e.add_field(name="Compte", value=compte.nom or "?", inline=True)
    e.add_field(name="Signatures enregistrées", value=f"{len(compte.signatures)}/{sig.MAX_SIGNATURES}", inline=True)
    e.add_field(name="Notifications", value="🔔 MP" if compte.notif else "🔕 coupées", inline=True)
    e.set_footer(text="/sws desinscription supprime tout (codes + signatures)")
    await inter.response.send_message(embed=e, ephemeral=True)


@sws_group.command(name="desinscription", description="Supprimer mes codes et signatures du bot")
async def sws_desinscription(inter: discord.Interaction) -> None:
    bot = _bot(inter)
    await bot.db.del_sws_compte(inter.user.id)
    cli = bot.sws_clients.pop(inter.user.id, None)
    if cli:
        await cli.fermer()
    await inter.response.send_message("🗑️ Codes et signatures supprimés du bot.", ephemeral=True)


# ── Surveillance (appelée par la boucle du bot) ─────────────────────────────────────────
async def surveiller(bot: "MenuCrous") -> None:
    now = datetime.now(TZ)
    for compte in await bot.db.sws_comptes():
        if not compte.notif:
            continue
        try:
            res = await client_de(bot, compte.user_id)
            if res is None:
                continue
            compte, cli = res
            items = await cli.a_signer()
        except Exception as exc:
            log.warning("SoWeSign (%s) : %s", compte.user_id, exc)
            continue
        cles = [a.cle for a in items]
        nouveaux = [a for a in items if a.cle not in compte.connus]
        rappel = (items and now.hour >= 19 and compte.rappel != now.date().isoformat())
        if nouveaux or rappel:
            titre = (f"🔔 Nouvelle signature à faire sur SoWeSign" if nouveaux else
                     f"⏰ Rappel : {len(items)} signature{'s' if len(items) > 1 else ''} en attente")
            try:
                user = await bot.fetch_user(compte.user_id)
                await user.send(embed=embed_attente(items, titre), view=BoutonsSignature(items))
            except discord.HTTPException as exc:
                log.warning("MP SoWeSign impossible (%s) : %s", compte.user_id, exc)
            if rappel:
                compte.rappel = now.date().isoformat()
        if cles != compte.connus or rappel:
            compte.connus = cles
            await bot.db.set_sws_compte(compte)


# ── Page web de dessin ──────────────────────────────────────────────────────────────────
def routes(bot: "MenuCrous") -> list[web.RouteDef]:
    def jeton_valide(j: str) -> int | None:
        v = bot.sws_jetons.get(j)
        if not v or v[1] < time.time():
            bot.sws_jetons.pop(j, None)
            return None
        return v[0]

    async def page(request: web.Request) -> web.Response:
        if jeton_valide(request.match_info["jeton"]) is None:
            return web.Response(text="Lien expiré : refais /sws signature sur Discord.", status=410)
        return web.Response(text=sig.PAGE_DESSIN.format(max=sig.MAX_SIGNATURES), content_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def enregistrer(request: web.Request) -> web.Response:
        uid = jeton_valide(request.match_info["jeton"])
        if uid is None:
            return web.json_response({"message": "Lien expiré : refais /sws signature."}, status=410)
        try:
            donnees = await request.json()
            png = sig.depuis_dataurl(str(donnees.get("signature", ""))[:2_000_000])
        except sig.SignatureInvalide as exc:
            return web.json_response({"message": f"❌ {exc}"}, status=400)
        except Exception:
            return web.json_response({"message": "❌ Requête invalide"}, status=400)
        compte = await bot.db.sws_compte(uid)
        if not compte:
            return web.json_response({"message": "Compte introuvable : /sws inscription."}, status=404)
        await _ajouter_signature(bot, compte, png)
        return web.json_response({"message": f"✅ Enregistrée ({len(compte.signatures)}/{sig.MAX_SIGNATURES}). "
                                              "Tu peux en ajouter une autre ou fermer la page."})

    return [web.get("/sws/signature/{jeton}", page),
            web.post("/sws/signature/{jeton}", enregistrer)]
