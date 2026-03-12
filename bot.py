import os
from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from scraper import get_menu

load_dotenv()

TOKEN: str = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID_STR: str = os.environ.get("CHANNEL_ID", "")

if not TOKEN:
    raise RuntimeError("Variable d'environnement DISCORD_TOKEN manquante.")
if not CHANNEL_ID_STR:
    raise RuntimeError("Variable d'environnement CHANNEL_ID manquante.")

CHANNEL_ID: int = int(CHANNEL_ID_STR)

PARIS_TZ = ZoneInfo("Europe/Paris")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@tasks.loop(time=time(7, 0, tzinfo=PARIS_TZ))
async def send_daily_menu() -> None:
    """Envoie automatiquement le menu chaque matin à 7h (heure de Paris)."""
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[ERREUR] Channel introuvable (id={CHANNEL_ID}). Vérifiez CHANNEL_ID dans .env")
        return
    print("📥 Récupération du menu du jour...")
    menu = await get_menu(date_offset=0)
    await _send_long_message(channel, menu)
    print("✅ Menu envoyé.")


@send_daily_menu.before_loop
async def before_loop() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    print(f"✅ Bot connecté : {bot.user} (id={bot.user.id})")
    print(f"📅 Envoi programmé chaque jour à 07h00 (heure de Paris)")
    synced = await bot.tree.sync()
    print(f"🔄 {len(synced)} slash command(s) synchronisée(s)")
    if not send_daily_menu.is_running():
        send_daily_menu.start()


# ── Commande préfixe : !menu ──────────────────────────────────────────────────
@bot.command(name="menu")
async def menu_prefix(ctx: commands.Context) -> None:
    """!menu — envoie immédiatement le menu du jour (commande préfixe)."""
    msg = await ctx.send("⏳ Récupération du menu en cours...")
    menu = await get_menu(date_offset=0)
    await msg.delete()
    await _send_long_message(ctx.channel, menu)


# ── Slash command : /menu ─────────────────────────────────────────────────────
@bot.tree.command(name="menu", description="Affiche le menu du jour — Manufacture des Tabacs (Lyon 3)")
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@discord.app_commands.describe(
    jour="Quel jour ? (défaut : aujourd'hui)",
    mode="Affichage : 'complet' pour voir aussi entrées et desserts",
)
@discord.app_commands.choices(jour=[
    discord.app_commands.Choice(name="Aujourd'hui", value=0),
    discord.app_commands.Choice(name="Demain", value=1),
    discord.app_commands.Choice(name="Après-demain", value=2),
])
async def menu_slash(
    interaction: discord.Interaction,
    jour: discord.app_commands.Choice[int] | None = None,
    mode: Literal["complet"] | None = None,
) -> None:
    complet = mode == "complet"
    date_offset = jour.value if jour is not None else 0
    await interaction.response.defer(thinking=True)
    menu = await get_menu(date_offset=date_offset, complet=complet)
    chunks = _split_message(menu)
    await interaction.followup.send(chunks[0])
    for chunk in chunks[1:]:
        await interaction.channel.send(chunk)


# ── Slash command : /help ─────────────────────────────────────────────
@bot.tree.command(name="help", description="Affiche l'aide du bot MenuCrous")
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def help_slash(interaction: discord.Interaction) -> None:
    """Liste toutes les commandes disponibles."""
    embed = discord.Embed(
        title="🍴 MenuCrous — Aide",
        description="Bot qui affiche le menu du restaurant CROUS Manufacture des Tabacs (Lyon 3)",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="`/menu`",
        value="Affiche les **plats et accompagnements** du jour (entrées et desserts masqués).",
        inline=False,
    )
    embed.add_field(
        name="`/menu jour:Demain`** / **`jour:Après-demain`",
        value="Affiche le menu d'un autre jour de la semaine en cours.",
        inline=False,
    )
    embed.add_field(
        name="`/menu mode:complet`",
        value="Affiche le menu **complet** : entrées, plats, accompagnements et desserts.",
        inline=False,
    )
    embed.add_field(
        name="`/help`",
        value="Affiche ce message d'aide.",
        inline=False,
    )
    embed.set_footer(text="Envoi automatique chaque matin à 7h00 🔔")
    await interaction.response.send_message(embed=embed, ephemeral=True)


def _split_message(text: str, limit: int = 1900) -> list[str]:
    """Découpe un texte en blocs n'excédant pas `limit` caractères."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


async def _send_long_message(channel: discord.abc.Messageable, text: str) -> None:
    """Envoie un message en le découpant si besoin (> 1900 caractères)."""
    for chunk in _split_message(text):
        await channel.send(chunk)
