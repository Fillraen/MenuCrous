"""Bot Discord MenuCrous : menus CROUS (Manufacture, Puvis) + alertes de notes INSA."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import menu as menus
from . import notes as notes_mod
from .config import RESTAURANTS, TZ, Settings
from .db import Database, MenuConfig, NotesConfig
from .format import ferme_embed, jour_fr, menu_embed

log = logging.getLogger("menucrous")


CHOIX_RESTO = [
    app_commands.Choice(name="Manufacture des Tabacs", value="manufacture"),
    app_commands.Choice(name="Puvis de Chavannes", value="puvis"),
    app_commands.Choice(name="Les deux", value="tous"),
]
CHOIX_JOUR = [
    app_commands.Choice(name="Aujourd'hui", value="0"),
    app_commands.Choice(name="Demain", value="1"),
    app_commands.Choice(name="Après-demain", value="2"),
    *[app_commands.Choice(name=f"{j.capitalize()} (prochain)", value=j)
      for j in ("lundi", "mardi", "mercredi", "jeudi", "vendredi")],
]
JOURS_IDX = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4}
# Fenêtre pendant laquelle l'envoi automatique est encore rattrapé après l'heure prévue
# (bot redémarré à 8h05 → il envoie quand même ; redémarré à 15h → il n'envoie plus).
RATTRAPAGE = timedelta(hours=3)


def maintenant() -> datetime:
    return datetime.now(TZ)


def resoudre_jour(valeur: str | None) -> date:
    today = maintenant().date()
    if not valeur:
        return today
    if valeur.isdigit():
        return today + timedelta(days=int(valeur))
    cible = JOURS_IDX[valeur]
    return today + timedelta(days=(cible - today.weekday()) % 7)


def restos(valeur: str | None) -> list:
    if not valeur or valeur == "tous":
        return list(RESTAURANTS.values())
    return [RESTAURANTS[valeur]]


def heure_valide(h: str) -> str | None:
    m = re.fullmatch(r"([01]?\d|2[0-3])[:hH]?([0-5]\d)?", h.strip())
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2) or '00'}"


class MenuCrous(commands.Bot):
    def __init__(self, settings: Settings):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(),
                         help_command=None)
        self.settings = settings
        self.db = Database(settings.data_dir / "menucrous.db")
        self.notes_etat: dict = {"dernier_passage": None, "erreur": None, "nb": 0}
        self.notes_lock = asyncio.Lock()
        self.demarrage = time.time()

    # ── Démarrage ─────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        await self.db.open()
        self.tree.add_command(config_group)
        self.tree.add_command(notes_group)
        for cmd in (menu_cmd, semaine_cmd, restaurants_cmd, aide_cmd):
            self.tree.add_command(cmd)
        synced = await self.tree.sync()
        log.info("%d commandes slash synchronisées", len(synced))
        self.envoi_menus.start()
        if self.settings.notes_enabled:
            self.surveille_notes.change_interval(minutes=self.settings.notes_interval_min)
            self.surveille_notes.start()
        else:
            log.info("INSA_USERNAME/INSA_PASSWORD absents : surveillance des notes désactivée")
        from .web import start_web  # import tardif : aiohttp ne sert qu'ici
        await start_web(self)

    async def on_ready(self) -> None:
        log.info("Connecté : %s (id=%s), %d serveur(s)", self.user, self.user.id, len(self.guilds))
        await self._config_initiale()
        await self.change_presence(activity=discord.Game("/menu · /aide"))

    async def _config_initiale(self) -> None:
        """Premier démarrage : reprend MENU_CHANNEL_ID / NOTES_CHANNEL_ID de l'environnement."""
        s = self.settings
        for cid, kind in ((s.default_menu_channel, "menu"), (s.default_notes_channel, "notes")):
            if not cid:
                continue
            ch = self.get_channel(cid)
            if not isinstance(ch, discord.abc.GuildChannel):
                log.warning("Salon %s (%s) introuvable : bot pas invité sur ce serveur ?", cid, kind)
                continue
            if kind == "menu" and not await self.db.menu_config(ch.guild.id):
                h = heure_valide(s.default_menu_time) or "08:00"
                await self.db.set_menu_config(
                    MenuConfig(ch.guild.id, cid, h, list(RESTAURANTS), False, True, None))
                log.info("Menu auto initialisé : #%s à %s", ch.name, h)
            if kind == "notes" and not await self.db.notes_config(ch.guild.id):
                await self.db.set_notes_config(NotesConfig(ch.guild.id, cid, True))
                log.info("Alertes notes initialisées : #%s", ch.name)

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    # ── Menus ─────────────────────────────────────────────────────────────────
    async def embeds_menu(self, liste, jour: date, complet: bool, montrer_fermes: bool = True):
        embeds = []
        for r in liste:
            try:
                m = await menus.get_menu(r, jour)
            except Exception as exc:
                embeds.append(discord.Embed(title=f"❌ {r.nom}", color=0xE74C3C,
                                            description=f"Menu injoignable (CROUS et CROUStillant) : {exc}"[:4000]))
                continue
            if m is None:
                if montrer_fermes:
                    embeds.append(ferme_embed(r.nom, jour))
                continue
            embeds.append(menu_embed(m if complet else menus.filtre_court(m), complet))
        return embeds

    async def poster_menu(self, cfg: MenuConfig, jour: date) -> bool:
        ch = self.get_channel(cfg.channel_id)
        if ch is None:
            try:
                ch = await self.fetch_channel(cfg.channel_id)
            except discord.HTTPException:
                log.warning("Salon menu %s introuvable", cfg.channel_id)
                return False
        liste = [RESTAURANTS[k] for k in cfg.restaurants if k in RESTAURANTS]
        embeds = await self.embeds_menu(liste, jour, cfg.complet, montrer_fermes=False)
        if not embeds:
            log.info("Pas de menu le %s (fermé) : rien envoyé dans #%s", jour, getattr(ch, "name", ch.id))
            return False
        await ch.send(content=f"🍴 **Menu du {jour_fr(jour)}**", embeds=embeds[:10])
        return True

    @tasks.loop(minutes=1)
    async def envoi_menus(self) -> None:
        now = maintenant()
        today = now.date().isoformat()
        for cfg in await self.db.menu_configs():
            if not cfg.actif or cfg.dernier_envoi == today:
                continue
            h, m = map(int, cfg.heure.split(":"))
            prevu = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if not (prevu <= now < prevu + RATTRAPAGE):
                continue
            try:
                await self.poster_menu(cfg, now.date())
            except Exception:
                log.exception("Envoi auto du menu (serveur %s)", cfg.guild_id)
                continue  # retenté la minute suivante
            await self.db.mark_menu_sent(cfg.guild_id, today)

    @envoi_menus.before_loop
    async def _avant_menus(self) -> None:
        await self.wait_until_ready()

    # ── Notes ─────────────────────────────────────────────────────────────────
    async def verifier_notes(self) -> tuple[list[notes_mod.Note], list[notes_mod.Note]]:
        """Renvoie (toutes les notes, nouvelles notes) et met à jour l'instantané."""
        s = self.settings
        async with self.notes_lock:
            try:
                toutes = await notes_mod.fetch_notes(
                    s.insa_username, s.insa_password, s.notes_url, s.data_dir / "debug")
            except Exception as exc:
                self.notes_etat.update(erreur=str(exc)[:500], dernier_passage=maintenant().isoformat())
                raise
            avant = await self.db.get_json("notes_snapshot")
            nouvelles = notes_mod.diff(avant or {}, toutes) if avant is not None else []
            await self.db.set_json("notes_snapshot", {n.cle: n.to_dict() for n in toutes})
            self.notes_etat.update(erreur=None, dernier_passage=maintenant().isoformat(),
                                   nb=sum(n.publiee for n in toutes))
            if avant is None:
                log.info("Premier relevé des notes : %d lignes mémorisées, pas d'alerte", len(toutes))
            return toutes, nouvelles

    async def annoncer_notes(self, nouvelles: list[notes_mod.Note]) -> None:
        if not nouvelles:
            return
        matieres = "\n".join(f"• **{n.matiere}**" for n in nouvelles)
        # 1) Salons configurés : la matière seulement, jamais la note
        for cfg in await self.db.notes_configs():
            if not cfg.actif:
                continue
            ch = self.get_channel(cfg.channel_id)
            if ch:
                await ch.send(embed=discord.Embed(
                    title="📢 Nouvelle(s) note(s) sur Mon Dossier Web", description=matieres,
                    color=0x9B59B6).set_footer(text="Allez vérifier sur mondossierweb.insa-lyon.fr"))
        # 2) MP au propriétaire avec les valeurs
        await self._mp_proprietaire(nouvelles)

    async def _mp_proprietaire(self, liste: list[notes_mod.Note]) -> None:
        if not self.settings.notes_owner_id:
            return
        try:
            user = await self.fetch_user(self.settings.notes_owner_id)
            await user.send(embed=notes_embed("🎓 Nouvelle(s) note(s)", liste, valeurs=True))
        except discord.HTTPException as exc:
            log.warning("MP au propriétaire impossible : %s", exc)

    @tasks.loop(minutes=30)
    async def surveille_notes(self) -> None:
        h = maintenant().hour
        if h < 7 or h >= 23:  # personne ne publie de notes la nuit
            return
        try:
            _, nouvelles = await self.verifier_notes()
        except Exception as exc:
            log.warning("Relevé des notes en échec : %s", exc)
            return
        await self.annoncer_notes(nouvelles)

    @surveille_notes.before_loop
    async def _avant_notes(self) -> None:
        await self.wait_until_ready()


def notes_embed(titre: str, liste: list[notes_mod.Note], valeurs: bool) -> discord.Embed:
    e = discord.Embed(title=titre, color=0x9B59B6, url="https://mondossierweb.insa-lyon.fr/mondossierweb/notes")
    lignes = []
    for n in liste:
        l = f"• **{n.matiere}**"
        if n.code:
            l += f" `{n.code}`"
        if valeurs:
            l += f" — **{n.valeur or '—'}**" + (f" ({n.resultat})" if n.resultat else "")
        lignes.append(l)
    desc = "\n".join(lignes) or "Aucune note."
    e.description = desc if len(desc) <= 4000 else desc[:3990] + "\n…"
    return e


def _bot(inter: discord.Interaction) -> MenuCrous:
    return inter.client  # type: ignore[return-value]


# ══ Commandes ═════════════════════════════════════════════════════════════════
@app_commands.command(name="menu", description="Menu du CROUS (Manufacture des Tabacs, Puvis de Chavannes)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(restaurant="Quel restaurant ? (défaut : les deux)", jour="Quel jour ? (défaut : aujourd'hui)",
                       complet="Afficher aussi les entrées et desserts")
@app_commands.choices(restaurant=CHOIX_RESTO, jour=CHOIX_JOUR)
async def menu_cmd(inter: discord.Interaction, restaurant: app_commands.Choice[str] | None = None,
                   jour: app_commands.Choice[str] | None = None, complet: bool = False) -> None:
    await inter.response.defer(thinking=True)
    d = resoudre_jour(jour.value if jour else None)
    embeds = await _bot(inter).embeds_menu(restos(restaurant.value if restaurant else None), d, complet)
    await inter.followup.send(embeds=embeds)


@app_commands.command(name="semaine", description="Plats de la semaine (résumé jour par jour)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(restaurant=CHOIX_RESTO[:2])
async def semaine_cmd(inter: discord.Interaction, restaurant: app_commands.Choice[str] | None = None) -> None:
    await inter.response.defer(thinking=True)
    r = RESTAURANTS[restaurant.value if restaurant else "manufacture"]
    jours = await menus.get_semaine(r)
    e = discord.Embed(title=f"📅 {r.nom} — menus à venir", url=r.crous_url, color=0xE67E22)
    for m in jours[:10]:
        plats = [p for rp in menus.filtre_court(m).repas for c in rp.categories
                 if c.nom.lower().startswith("plat") for p in c.plats]
        e.add_field(name=jour_fr(m.jour).capitalize(), value=("\n".join(f"• {p}" for p in plats) or "—")[:1024],
                    inline=False)
    if not jours:
        e.description = "Aucun menu publié pour les jours à venir."
    await inter.followup.send(embed=e)


@app_commands.command(name="restaurants", description="Horaires, adresse et accès des restaurants")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def restaurants_cmd(inter: discord.Interaction) -> None:
    await inter.response.defer(thinking=True)
    embeds = []
    for r in RESTAURANTS.values():
        e = discord.Embed(title=f"🏛️ {r.nom}", url=r.crous_url, color=0x1ABC9C)
        try:
            i = await menus.get_infos(r)
            e.add_field(name="Adresse", value=i.get("adresse") or "—", inline=False)
            e.add_field(name="Horaires", value="\n".join(i.get("horaires") or ["—"]), inline=False)
            if i.get("acces"):
                e.add_field(name="Accès", value="\n".join(f"• {a}" for a in i["acces"]), inline=False)
            if i.get("paiement"):
                e.add_field(name="Paiement", value=", ".join(i["paiement"]), inline=True)
            e.add_field(name="Ouvert", value="✅" if i.get("ouvert") else "❌", inline=True)
        except Exception as exc:
            e.description = f"Infos indisponibles : {exc}"
        embeds.append(e)
    await inter.followup.send(embeds=embeds)


@app_commands.command(name="aide", description="Toutes les commandes de MenuCrous")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def aide_cmd(inter: discord.Interaction) -> None:
    s = _bot(inter).settings
    e = discord.Embed(title="🍴 MenuCrous — aide", color=discord.Color.orange(),
                      description="Menus des restaurants CROUS **Manufacture des Tabacs** et **Puvis de Chavannes**, "
                                  "et alertes de nouvelles notes INSA.")
    e.add_field(name="Menus", inline=False, value=(
        "`/menu` — menu du jour (plats + accompagnements)\n"
        "`/menu restaurant: jour: complet:True` — autre resto / jour, entrées et desserts\n"
        "`/semaine` — les plats des prochains jours\n"
        "`/restaurants` — horaires, adresse, accès"))
    e.add_field(name="Notes INSA", inline=False, value=(
        "`/notes verifier` — regarde tout de suite s'il y a une nouvelle note\n"
        "`/notes statut` — dernier relevé, prochaine vérification\n"
        "`/notes liste` — toutes les notes (propriétaire, en privé)"))
    e.add_field(name="Réglages (gérer le serveur)", inline=False, value=(
        "`/config menu salon: heure: restaurants: complet:` — envoi automatique quotidien\n"
        "`/config menu-stop` · `/config notes salon:` · `/config notes-stop`\n"
        "`/config voir` · `/config test`"))
    if s.public_url:
        e.add_field(name="Sur le web", value=s.public_url, inline=False)
    e.set_footer(text="Sources : crous-lyon.fr, secours CROUStillant · Hébergé sur le homelab")
    await inter.response.send_message(embed=e, ephemeral=True)


# ── /config ───────────────────────────────────────────────────────────────────
config_group = app_commands.Group(
    name="config", description="Réglages MenuCrous pour ce serveur",
    default_permissions=discord.Permissions(manage_guild=True),
    allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
)


@config_group.command(name="menu", description="Envoi automatique du menu chaque jour dans un salon")
@app_commands.describe(salon="Salon où poster (défaut : celui-ci)", heure="Heure d'envoi, ex. 08:00 (heure de Paris)",
                       restaurants="Restaurants à inclure", complet="Inclure entrées et desserts")
@app_commands.choices(restaurants=CHOIX_RESTO)
async def config_menu(inter: discord.Interaction, salon: discord.TextChannel | None = None, heure: str | None = None,
                      restaurants: app_commands.Choice[str] | None = None, complet: bool | None = None) -> None:
    db = _bot(inter).db
    ancien = await db.menu_config(inter.guild_id)
    h = heure_valide(heure) if heure else (ancien.heure if ancien else "08:00")
    if h is None:
        await inter.response.send_message("❌ Heure invalide. Exemples : `08:00`, `8h`, `11h30`.", ephemeral=True)
        return
    ch = salon or inter.channel
    perms = ch.permissions_for(inter.guild.me)
    if not (perms.send_messages and perms.embed_links and perms.view_channel):
        await inter.response.send_message(f"❌ Je n'ai pas le droit d'écrire (avec embeds) dans {ch.mention}.",
                                          ephemeral=True)
        return
    cfg = MenuConfig(
        inter.guild_id, ch.id, h,
        [r.key for r in restos(restaurants.value)] if restaurants else (ancien.restaurants if ancien else list(RESTAURANTS)),
        complet if complet is not None else (ancien.complet if ancien else False),
        True,
        # Si on règle une heure déjà passée aujourd'hui, ne pas envoyer immédiatement
        maintenant().date().isoformat() if ancien and ancien.dernier_envoi == maintenant().date().isoformat() else None,
    )
    await db.set_menu_config(cfg)
    noms = ", ".join(RESTAURANTS[k].nom for k in cfg.restaurants)
    await inter.response.send_message(
        f"✅ Menu posté chaque jour à **{cfg.heure}** dans {ch.mention} ({noms}"
        f"{', complet' if cfg.complet else ''}). Rien n'est posté les jours sans menu.", ephemeral=True)


@config_group.command(name="menu-stop", description="Arrêter l'envoi automatique du menu")
async def config_menu_stop(inter: discord.Interaction) -> None:
    db = _bot(inter).db
    cfg = await db.menu_config(inter.guild_id)
    if not cfg or not cfg.actif:
        await inter.response.send_message("L'envoi automatique n'est pas activé ici.", ephemeral=True)
        return
    cfg.actif = False
    await db.set_menu_config(cfg)
    await inter.response.send_message("🛑 Envoi automatique du menu arrêté.", ephemeral=True)


@config_group.command(name="notes", description="Annoncer les nouvelles notes (matière seulement) dans un salon")
@app_commands.describe(salon="Salon des annonces (défaut : celui-ci)")
async def config_notes(inter: discord.Interaction, salon: discord.TextChannel | None = None) -> None:
    bot = _bot(inter)
    ch = salon or inter.channel
    await bot.db.set_notes_config(NotesConfig(inter.guild_id, ch.id, True))
    extra = "" if bot.settings.notes_enabled else "\n⚠️ Identifiants INSA absents du serveur : aucune note ne sera relevée."
    await inter.response.send_message(
        f"✅ Les nouvelles notes seront annoncées dans {ch.mention} (matière uniquement, jamais la note).{extra}",
        ephemeral=True)


@config_group.command(name="notes-stop", description="Ne plus annoncer les nouvelles notes sur ce serveur")
async def config_notes_stop(inter: discord.Interaction) -> None:
    db = _bot(inter).db
    cfg = await db.notes_config(inter.guild_id)
    if cfg:
        cfg.actif = False
        await db.set_notes_config(cfg)
    await inter.response.send_message("🛑 Annonces de notes coupées sur ce serveur.", ephemeral=True)


@config_group.command(name="voir", description="Afficher les réglages de ce serveur")
async def config_voir(inter: discord.Interaction) -> None:
    bot = _bot(inter)
    m = await bot.db.menu_config(inter.guild_id)
    n = await bot.db.notes_config(inter.guild_id)
    e = discord.Embed(title="⚙️ Réglages MenuCrous", color=0x95A5A6)
    e.add_field(name="Menu automatique", inline=False, value=(
        f"{'✅' if m.actif else '🛑'} <#{m.channel_id}> à **{m.heure}** — "
        f"{', '.join(RESTAURANTS[k].nom for k in m.restaurants if k in RESTAURANTS)}"
        f"{' (complet)' if m.complet else ''}\nDernier envoi : {m.dernier_envoi or 'jamais'}") if m else "Non configuré")
    e.add_field(name="Annonces de notes", inline=False,
                value=f"{'✅' if n.actif else '🛑'} <#{n.channel_id}>" if n else "Non configuré")
    e.add_field(name="Relevé des notes", inline=False, value=(
        f"Toutes les {bot.settings.notes_interval_min} min (7h–23h)" if bot.settings.notes_enabled
        else "Désactivé (identifiants INSA absents)"))
    await inter.response.send_message(embed=e, ephemeral=True)


@config_group.command(name="test", description="Poster le menu du jour maintenant dans le salon configuré")
async def config_test(inter: discord.Interaction) -> None:
    bot = _bot(inter)
    cfg = await bot.db.menu_config(inter.guild_id)
    if not cfg:
        await inter.response.send_message("Configure d'abord `/config menu`.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    ok = await bot.poster_menu(cfg, maintenant().date())
    await inter.followup.send("✅ Menu posté." if ok else "📭 Pas de menu aujourd'hui (rien posté).", ephemeral=True)


# ── /notes ────────────────────────────────────────────────────────────────────
notes_group = app_commands.Group(
    name="notes", description="Notes INSA (Mon Dossier Web)",
    allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
)


def _proprio(inter: discord.Interaction) -> bool:
    return inter.user.id == _bot(inter).settings.notes_owner_id


@notes_group.command(name="verifier", description="Regarder maintenant s'il y a une nouvelle note")
async def notes_verifier(inter: discord.Interaction) -> None:
    bot = _bot(inter)
    if not bot.settings.notes_enabled:
        await inter.response.send_message("La surveillance des notes n'est pas configurée sur ce bot.", ephemeral=True)
        return
    await inter.response.defer(thinking=True)
    try:
        _, nouvelles = await bot.verifier_notes()
    except Exception as exc:
        await inter.followup.send(f"❌ Relevé impossible : {str(exc)[:300]}")
        return
    if not nouvelles:
        await inter.followup.send("🤷 Aucune nouvelle note depuis le dernier relevé.")
        return
    await inter.followup.send(embed=notes_embed("📢 Nouvelle(s) note(s)", nouvelles, valeurs=False))
    # Annonces habituelles (salons + MP) : ce relevé a « consommé » les nouveautés
    await bot.annoncer_notes(nouvelles)


@notes_group.command(name="statut", description="Dernier relevé des notes et état de la surveillance")
async def notes_statut(inter: discord.Interaction) -> None:
    bot = _bot(inter)
    s, et = bot.settings, bot.notes_etat
    if not s.notes_enabled:
        await inter.response.send_message("La surveillance des notes n'est pas configurée sur ce bot.", ephemeral=True)
        return
    e = discord.Embed(title="🎓 Surveillance des notes", color=0x9B59B6)
    e.add_field(name="Fréquence", value=f"toutes les {s.notes_interval_min} min, de 7h à 23h")
    dp = et["dernier_passage"]
    e.add_field(name="Dernier relevé", value=f"<t:{int(datetime.fromisoformat(dp).timestamp())}:R>" if dp else "pas encore")
    nxt = bot.surveille_notes.next_iteration
    if nxt:
        e.add_field(name="Prochain", value=f"<t:{int(nxt.timestamp())}:R>")
    e.add_field(name="Notes publiées", value=str(et["nb"]))
    if et["erreur"]:
        e.add_field(name="⚠️ Dernière erreur", value=et["erreur"][:1000], inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)


@notes_group.command(name="liste", description="Toutes mes notes (propriétaire du bot uniquement, en privé)")
async def notes_liste(inter: discord.Interaction) -> None:
    bot = _bot(inter)
    if not _proprio(inter):
        await inter.response.send_message("🔒 Réservé au propriétaire du compte surveillé.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    snap = await bot.db.get_json("notes_snapshot")
    if snap is None:
        try:
            await bot.verifier_notes()
        except Exception as exc:
            await inter.followup.send(f"❌ {exc}", ephemeral=True)
            return
        snap = await bot.db.get_json("notes_snapshot", {})
    liste = [notes_mod.Note(**d) for d in snap.values() if d.get("valeur")]
    await inter.followup.send(embed=notes_embed(f"🎓 Mes notes ({len(liste)})", liste, valeurs=True), ephemeral=True)
