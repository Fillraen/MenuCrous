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

from . import edt as edt_mod
from . import menu as menus
from . import notes as notes_mod
from .config import RESTAURANTS, TZ, Settings
from .db import Database, EdtConfig, MenuConfig, NotesConfig
from .format import (edt_changements_embed, edt_jour_embed, edt_semaine_embed, ferme_embed, jour_fr,
                     menu_embed)

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
        # 2FA : code attendu du propriétaire (/notes code), et pause du relevé auto tant
        # qu'un code a été demandé sans réponse (pour ne pas le harceler toutes les 30 min).
        self.code_attendu: asyncio.Future[str] | None = None
        self.notes_en_pause = False
        self.demarrage = time.time()

    # ── Démarrage ─────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        await self.db.open()
        self.add_view(DetailNotes())  # bouton « Détail » des anciens messages, après redémarrage
        self.tree.add_command(config_group)
        self.tree.add_command(notes_group)
        self.tree.add_command(edt_group)
        for cmd in (menu_cmd, semaine_cmd, restaurants_cmd, aide_cmd):
            self.tree.add_command(cmd)
        synced = await self.tree.sync()
        log.info("%d commandes slash synchronisées", len(synced))
        self.envoi_menus.start()
        if self.settings.edt_ics_url:
            self.envoi_edt.start()
            self.surveille_edt.start()
        else:
            log.info("EDT_ICS_URL absent : emploi du temps désactivé")
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

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Ajouté au serveur %s (%s)", guild.name, guild.id)
        await self._config_initiale()

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

    # ── Emploi du temps ───────────────────────────────────────────────────────
    async def _salon(self, cid: int):
        ch = self.get_channel(cid)
        if ch is None:
            try:
                ch = await self.fetch_channel(cid)
            except discord.HTTPException:
                log.warning("Salon %s introuvable", cid)
        return ch

    async def poster_edt(self, cfg: EdtConfig, quoi: str, d: date) -> bool:
        """quoi = "semaine" (semaine contenant d) ou "jour". Renvoie False si rien posté."""
        ch = await self._salon(cfg.channel_id)
        if ch is None:
            return False
        cours = await edt_mod.get_cours(self.settings.edt_ics_url)
        if quoi == "semaine":
            await ch.send(embed=edt_semaine_embed(edt_mod.de_la_semaine(cours, d)))
            return True
        du_jour = edt_mod.du_jour(cours, d)
        if not du_jour:
            return False  # pas de cours : rien à poster
        await ch.send(embed=edt_jour_embed(d, du_jour))
        return True

    @tasks.loop(minutes=1)
    async def envoi_edt(self) -> None:
        now = maintenant()
        today = now.date()
        lundi_iso = edt_mod.lundi(today).isoformat()
        for cfg in await self.db.edt_configs():
            if not cfg.actif:
                continue
            h, m = map(int, cfg.heure.split(":"))
            prevu = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if not (prevu <= now < prevu + RATTRAPAGE):
                continue
            try:
                if (cfg.mode in ("semaine", "les2") and today.weekday() == 0
                        and cfg.derniere_semaine != lundi_iso):
                    await self.poster_edt(cfg, "semaine", today)
                    cfg.derniere_semaine = lundi_iso
                    await self.db.set_edt_config(cfg)
                if cfg.mode in ("jour", "les2") and cfg.dernier_jour != today.isoformat():
                    # « les2 » le lundi : le récap de la semaine suffit
                    if not (cfg.mode == "les2" and today.weekday() == 0):
                        await self.poster_edt(cfg, "jour", today)
                    cfg.dernier_jour = today.isoformat()
                    await self.db.set_edt_config(cfg)
            except Exception:
                log.exception("Envoi auto de l'EDT (serveur %s)", cfg.guild_id)

    @tasks.loop(minutes=30)
    async def surveille_edt(self) -> None:
        """Cours des 14 prochains jours déplacés / ajoutés / annulés."""
        now = maintenant()
        if now.hour < 7 or now.hour >= 23:
            return
        try:
            cours = await edt_mod.get_cours(self.settings.edt_ics_url, force=True)
        except Exception as exc:
            log.warning("Lecture de l'EDT en échec : %s", exc)
            return
        today = now.date()
        snap = await self.db.get_json("edt_snapshot")
        await self.db.set_json("edt_snapshot", edt_mod.instantane(cours, today))
        if not snap or "fin" not in snap:
            log.info("Premier relevé de l'EDT : %d cours mémorisés", len(cours))
            return
        chg = edt_mod.changements(snap, cours, today)
        if not chg:
            return
        log.info("EDT : %d changement(s)", len(chg))
        embed = edt_changements_embed(chg)
        for cfg in await self.db.edt_configs():
            if cfg.actif and cfg.modifs and (ch := await self._salon(cfg.channel_id)):
                await ch.send(embed=embed)

    @envoi_edt.before_loop
    @surveille_edt.before_loop
    async def _avant_edt(self) -> None:
        await self.wait_until_ready()

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
                    s.insa_username, s.insa_password, s.notes_url, s.data_dir / "debug",
                    session_file=s.data_dir / "insa-session.json", totp_secret=s.insa_totp_secret,
                    demande_code=self._demander_code)
                self.notes_en_pause = False
            except notes_mod.DeuxFacteursRequis as exc:
                self.notes_en_pause = True
                self.notes_etat.update(erreur=f"2FA : {exc}", dernier_passage=maintenant().isoformat())
                raise
            except Exception as exc:
                self.notes_etat.update(erreur=str(exc)[:500], dernier_passage=maintenant().isoformat())
                raise
            avant = await self.db.get_json("notes_snapshot")
            nouvelles = notes_mod.diff(avant or {}, toutes) if avant is not None else []
            await self.db.set_json("notes_snapshot", notes_mod.instantane(toutes))
            await self.db.set_json("notes_resume", notes_mod.resume(toutes))
            self.notes_etat.update(erreur=None, dernier_passage=maintenant().isoformat(),
                                   nb=sum(n.publiee and n.detail for n in toutes))
            if avant is None:
                log.info("Premier relevé des notes : %d lignes mémorisées, pas d'alerte", len(toutes))
            return toutes, nouvelles

    async def _demander_code(self, consigne: str) -> str | None:
        """Session INSA expirée et 2FA demandé : MP au propriétaire, attente de /notes code."""
        if not self.settings.notes_owner_id:
            return None
        loop = asyncio.get_running_loop()
        self.code_attendu = loop.create_future()
        try:
            user = await self.fetch_user(self.settings.notes_owner_id)
            await user.send(embed=discord.Embed(
                title="🔐 Code 2FA INSA demandé", color=0xF1C40F,
                description=(f"La session Mon Dossier Web a expiré, l'INSA demande un code :\n> {consigne[:300]}\n\n"
                             "Réponds avec **`/notes code 123456`** dans les 10 minutes.\n"
                             "Astuce : avec `INSA_TOTP_SECRET` dans le .env, le bot génère le code tout seul.")))
            return await asyncio.wait_for(self.code_attendu, timeout=600)
        except (asyncio.TimeoutError, discord.HTTPException) as exc:
            log.warning("Code 2FA non reçu : %s", exc)
            return None
        finally:
            self.code_attendu = None

    async def annoncer_notes(self, nouvelles: list[notes_mod.Note], sauf_salon: int | None = None) -> None:
        """Annonce les matières qui ont une nouvelle note. Jamais la note elle-même."""
        if not nouvelles:
            return
        res = await self.db.get_json("notes_resume", [])
        embed = notes_embed(notes_mod.matieres(nouvelles), res)
        log.info("Nouvelle(s) note(s) : %d ligne(s), %d matière(s)", len(nouvelles), len(notes_mod.matieres(nouvelles)))
        envoye = False
        for cfg in await self.db.notes_configs():
            if not cfg.actif or cfg.channel_id == sauf_salon:
                continue
            ch = self.get_channel(cfg.channel_id)
            if ch:
                await ch.send(embed=embed, view=DetailNotes(res))
                envoye = True
        # Aucun salon configuré : MP au propriétaire, pour ne pas rater l'info
        if not envoye and sauf_salon is None and self.settings.notes_owner_id:
            try:
                await (await self.fetch_user(self.settings.notes_owner_id)).send(embed=embed, view=DetailNotes(res))
                log.info("Annonce envoyée en MP au propriétaire")
            except discord.HTTPException as exc:
                log.warning("MP au propriétaire impossible : %s", exc)

    @tasks.loop(minutes=30)
    async def surveille_notes(self) -> None:
        h = maintenant().hour
        if h < 7 or h >= 23 or self.notes_en_pause:  # nuit, ou 2FA en attente de /notes verifier
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


MDW_URL = "https://mondossierweb.insa-lyon.fr/mondossierweb/inscriptions"


def ajouter_recap(e: discord.Embed, res: list[dict]) -> discord.Embed:
    """Une ligne par année : nombre de notes publiées / attendues (jamais les notes)."""
    for annee, (pub, tot) in notes_mod.compte_par_annee(res).items():
        barre = "▰" * round(10 * pub / tot) + "▱" * (10 - round(10 * pub / tot)) if tot else ""
        e.add_field(name=f"🎓 {annee}", value=f"**{pub}** note{'s' if pub > 1 else ''} / {tot}\n{barre}", inline=True)
    return e


def notes_embed(matieres: list[str], res: list[dict]) -> discord.Embed:
    desc = "\n".join(f"• **{m}**" for m in matieres)
    e = discord.Embed(
        title="📢 Nouvelle note sur Mon Dossier Web", color=0x9B59B6, url=MDW_URL,
        description=desc if len(desc) <= 4000 else desc[:3990] + "\n…",
    ).set_footer(text="Parcours → Notes et résultats")
    return ajouter_recap(e, res)


def detail_embed(res: list[dict]) -> discord.Embed:
    """Matières de l'année la plus récente : ✅ note(s) dispo / ▫️ en attente."""
    if not res:
        return discord.Embed(description="Pas encore de relevé des notes.", color=0x9B59B6)
    annee = res[0]["annee"]  # Mon Dossier Web liste l'année la plus récente en premier
    pub, tot = notes_mod.compte_par_annee(res).get(annee, (0, 0))
    e = discord.Embed(title=f"🎓 {annee} — {pub} note{'s' if pub > 1 else ''} sur {tot}", color=0x9B59B6,
                      url=MDW_URL, description="✅ note disponible · ▫️ pas encore de note")
    for semestre, matieres in notes_mod.detail_annee(res, annee).items():
        lignes = []
        for mat, p, t in matieres:
            icone = "✅" if p else "▫️"
            compte = f" ({p}/{t})" if t > 1 else ""
            lignes.append(f"{icone} {mat}{compte}")
        # Découpe en plusieurs champs si besoin (1024 caractères max par champ)
        bloc, n = [], 0
        for l in lignes:
            if sum(len(x) + 1 for x in bloc) + len(l) > 1000:
                e.add_field(name=semestre + (" (suite)" if n else ""), value="\n".join(bloc), inline=False)
                bloc, n = [], n + 1
            bloc.append(l)
        if bloc:
            e.add_field(name=semestre + (" (suite)" if n else ""), value="\n".join(bloc), inline=False)
    return e.set_footer(text="Les notes elles-mêmes ne sont jamais affichées")


class DetailNotes(discord.ui.View):
    """Bouton « Détail » sous les messages de notes. Persistant : ``custom_id`` fixe et vue
    enregistrée au démarrage (add_view), il marche encore après un redémarrage du bot."""

    def __init__(self, res: list[dict] | None = None):
        super().__init__(timeout=None)
        if res:
            self.detail.label = f"Détail {res[0]['annee']}"

    @discord.ui.button(label="Détail de l'année", emoji="📋", style=discord.ButtonStyle.secondary,
                       custom_id="menucrous:notes-detail")
    async def detail(self, inter: discord.Interaction, _button: discord.ui.Button) -> None:
        res = await _bot(inter).db.get_json("notes_resume", [])
        await inter.response.send_message(embed=detail_embed(res), ephemeral=True)


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
        "`/notes code` — donner le code 2FA INSA demandé en MP (propriétaire)"))
    e.add_field(name="Emploi du temps", inline=False, value=(
        "`/edt jour quand:` — cours d'aujourd'hui, demain ou d'un jour de la semaine\n"
        "`/edt semaine quand:` — cette semaine ou la suivante"))
    e.add_field(name="Réglages (gérer le serveur)", inline=False, value=(
        "`/config menu salon: heure: restaurants: complet:` — envoi automatique quotidien\n"
        "`/config edt salon: mode: heure: modifs:` — EDT de la semaine (lundi) et/ou du jour\n"
        "`/config notes salon:` — annonces de nouvelles notes\n"
        "`/config menu-stop` · `/config edt-stop` · `/config notes-stop`\n"
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


MODES_EDT = {"semaine": "semaine le lundi", "jour": "chaque jour de cours", "les2": "semaine le lundi + chaque jour"}


@config_group.command(name="edt", description="Envoi automatique de l'emploi du temps dans un salon")
@app_commands.describe(salon="Salon où poster (défaut : celui-ci)",
                       mode="Récap de la semaine le lundi, cours du jour chaque matin, ou les deux",
                       heure="Heure d'envoi, ex. 07:30 (heure de Paris)",
                       modifs="Annoncer les cours déplacés / ajoutés / annulés (14 prochains jours)")
@app_commands.choices(mode=[
    app_commands.Choice(name="Semaine (le lundi)", value="semaine"),
    app_commands.Choice(name="Jour (chaque matin de cours)", value="jour"),
    app_commands.Choice(name="Les deux", value="les2"),
])
async def config_edt(inter: discord.Interaction, salon: discord.TextChannel | None = None,
                     mode: app_commands.Choice[str] | None = None, heure: str | None = None,
                     modifs: bool | None = None) -> None:
    bot = _bot(inter)
    if not bot.settings.edt_ics_url:
        await inter.response.send_message("❌ Pas de lien ADE (`EDT_ICS_URL`) sur le serveur.", ephemeral=True)
        return
    ancien = await bot.db.edt_config(inter.guild_id)
    h = heure_valide(heure) if heure else (ancien.heure if ancien else "07:30")
    if h is None:
        await inter.response.send_message("❌ Heure invalide. Exemples : `07:30`, `7h`, `8h15`.", ephemeral=True)
        return
    ch = salon or inter.channel
    perms = ch.permissions_for(inter.guild.me)
    if not (perms.send_messages and perms.embed_links and perms.view_channel):
        await inter.response.send_message(f"❌ Je n'ai pas le droit d'écrire (avec embeds) dans {ch.mention}.",
                                          ephemeral=True)
        return
    cfg = EdtConfig(
        inter.guild_id, ch.id, mode.value if mode else (ancien.mode if ancien else "semaine"), h,
        modifs if modifs is not None else (ancien.modifs if ancien else True), True,
        # Pas d'envoi immédiat si l'heure est déjà passée et que c'était déjà envoyé
        ancien.dernier_jour if ancien else None, ancien.derniere_semaine if ancien else None,
    )
    await bot.db.set_edt_config(cfg)
    await inter.response.send_message(
        f"✅ Emploi du temps dans {ch.mention} : **{MODES_EDT[cfg.mode]}** à **{cfg.heure}**"
        f"{', + alertes de modifications' if cfg.modifs else ''}. Rien n'est posté les jours sans cours.\n"
        f"Aperçu tout de suite : `/edt semaine` ou `/edt jour`.", ephemeral=True)


@config_group.command(name="edt-stop", description="Arrêter l'envoi automatique de l'emploi du temps")
async def config_edt_stop(inter: discord.Interaction) -> None:
    db = _bot(inter).db
    cfg = await db.edt_config(inter.guild_id)
    if not cfg or not cfg.actif:
        await inter.response.send_message("L'envoi de l'emploi du temps n'est pas activé ici.", ephemeral=True)
        return
    cfg.actif = False
    await db.set_edt_config(cfg)
    await inter.response.send_message("🛑 Envoi automatique de l'emploi du temps arrêté.", ephemeral=True)


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
    t = await bot.db.edt_config(inter.guild_id)
    e.add_field(name="Emploi du temps", inline=False, value=(
        f"{'✅' if t.actif else '🛑'} <#{t.channel_id}> — {MODES_EDT[t.mode]} à **{t.heure}**"
        f"{' · modifications annoncées' if t.modifs else ''}") if t else "Non configuré")
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


# ── /edt ──────────────────────────────────────────────────────────────────────
edt_group = app_commands.Group(
    name="edt", description="Emploi du temps (ADE INSA)",
    allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
)


async def _cours_ou_erreur(inter: discord.Interaction) -> list | None:
    bot = _bot(inter)
    if not bot.settings.edt_ics_url:
        await inter.followup.send("❌ Pas de lien ADE (`EDT_ICS_URL`) configuré sur le serveur.")
        return None
    try:
        return await edt_mod.get_cours(bot.settings.edt_ics_url)
    except Exception as exc:
        await inter.followup.send(f"❌ Emploi du temps injoignable : {str(exc)[:300]}")
        return None


@edt_group.command(name="jour", description="Cours d'un jour (défaut : aujourd'hui)")
@app_commands.describe(quand="Quel jour ?")
@app_commands.choices(quand=CHOIX_JOUR)
async def edt_jour(inter: discord.Interaction, quand: app_commands.Choice[str] | None = None) -> None:
    await inter.response.defer(thinking=True)
    cours = await _cours_ou_erreur(inter)
    if cours is None:
        return
    d = resoudre_jour(quand.value if quand else None)
    await inter.followup.send(embed=edt_jour_embed(d, edt_mod.du_jour(cours, d)))


@edt_group.command(name="semaine", description="Emploi du temps de la semaine")
@app_commands.describe(quand="Quelle semaine ?")
@app_commands.choices(quand=[
    app_commands.Choice(name="Cette semaine", value="0"),
    app_commands.Choice(name="Semaine prochaine", value="1"),
    app_commands.Choice(name="Dans 2 semaines", value="2"),
])
async def edt_semaine(inter: discord.Interaction, quand: app_commands.Choice[str] | None = None) -> None:
    await inter.response.defer(thinking=True)
    cours = await _cours_ou_erreur(inter)
    if cours is None:
        return
    d = maintenant().date()
    if quand is None and d.weekday() >= 5:
        d += timedelta(days=2)  # le week-end, « cette semaine » = celle qui arrive
    d += timedelta(weeks=int(quand.value) if quand else 0)
    await inter.followup.send(embed=edt_semaine_embed(edt_mod.de_la_semaine(cours, d)))


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
    except notes_mod.DeuxFacteursRequis:
        await inter.followup.send("🔐 L'INSA demande un code 2FA : le propriétaire doit répondre en MP "
                                  "(`/notes code`), puis relancer `/notes verifier`.")
        return
    except Exception as exc:
        await inter.followup.send(f"❌ Relevé impossible : {str(exc)[:300]}")
        return
    res = await bot.db.get_json("notes_resume", [])
    if not nouvelles:
        e = discord.Embed(title="🤷 Aucune nouvelle note depuis le dernier relevé", color=0x9B59B6, url=MDW_URL)
        await inter.followup.send(embed=ajouter_recap(e, res), view=DetailNotes(res))
        return
    await inter.followup.send(embed=notes_embed(notes_mod.matieres(nouvelles), res), view=DetailNotes(res))
    # Annonce aussi dans les salons configurés (ce relevé a « consommé » les nouveautés)
    await bot.annoncer_notes(nouvelles, sauf_salon=inter.channel_id or 0)


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
    if et["erreur"]:
        e.add_field(name="⚠️ Dernière erreur", value=et["erreur"][:1000], inline=False)
    res = await bot.db.get_json("notes_resume", [])
    await inter.response.send_message(embed=ajouter_recap(e, res), view=DetailNotes(res), ephemeral=True)


@notes_group.command(name="code", description="Donner le code 2FA INSA demandé en MP (propriétaire)")
@app_commands.describe(code="Code à 6 chiffres reçu / affiché par ton appli 2FA")
async def notes_code(inter: discord.Interaction, code: str) -> None:
    bot = _bot(inter)
    if not _proprio(inter):
        await inter.response.send_message("🔒 Réservé au propriétaire du compte surveillé.", ephemeral=True)
        return
    fut = bot.code_attendu
    if fut is None or fut.done():
        await inter.response.send_message(
            "Aucun code attendu en ce moment. Lance `/notes verifier` : si l'INSA demande un code, "
            "je te le redemande.", ephemeral=True)
        return
    fut.set_result(code.strip())
    await inter.response.send_message("✅ Code transmis, connexion en cours…", ephemeral=True)
