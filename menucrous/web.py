"""Petite page web publique : menus de la semaine, API JSON et /health (healthcheck Docker)."""
from __future__ import annotations

import html
import logging
import time
from datetime import date
from typing import TYPE_CHECKING

from aiohttp import web

from . import menu as menus
from .config import RESTAURANTS
from .format import _rang, jour_fr

if TYPE_CHECKING:
    from .bot import MenuCrous

log = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MenuCrous — menus CROUS Lyon</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f4ef;--card:#fff;--fg:#1d1d1f;--mut:#6b6b6b;--acc:#d35400}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16161a;--card:#202026;--fg:#ececec;--mut:#9a9aa2;--acc:#f39c4a}}}}
body{{margin:0;font:16px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:1100px;margin:auto;padding:1.5rem 1rem 3rem}}
h1{{margin:.2rem 0}} .sub{{color:var(--mut);margin:0 0 1.5rem}}
.tabs{{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem}}
.tabs a{{padding:.35rem .8rem;border-radius:999px;background:var(--card);color:var(--fg);text-decoration:none}}
.tabs a.on{{background:var(--acc);color:#fff}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1rem}}
.card{{background:var(--card);border-radius:14px;padding:1rem 1.2rem;box-shadow:0 1px 3px #0002}}
.card h2{{font-size:1.15rem;margin:0 0 .5rem}} .card h2 a{{color:inherit}}
h3{{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:var(--acc);margin:.8rem 0 .2rem}}
ul{{margin:0;padding-left:1.1rem}} .vide{{color:var(--mut)}}
footer{{margin-top:2rem;color:var(--mut);font-size:.85rem}}
</style></head><body><main>
<h1>🍴 MenuCrous</h1>
<p class="sub">Menus des restaurants CROUS de Lyon — bot Discord <code>/menu</code></p>
<nav class="tabs">{tabs}</nav>
<div class="grid">{cards}</div>
<footer>Sources : crous-lyon.fr, secours CROUStillant · API : <a href="/api/menu">/api/menu</a></footer>
</main></body></html>"""


def _card(m: menus.MenuJour | None, nom: str, url: str) -> str:
    e = html.escape
    if m is None:
        return f'<section class="card"><h2><a href="{e(url)}">{e(nom)}</a></h2><p class="vide">Pas de menu ce jour-là.</p></section>'
    parts = [f'<section class="card"><h2><a href="{e(url)}">{e(nom)}</a></h2>']
    for rp in m.repas:
        if len(m.repas) > 1:
            parts.append(f"<h3>— {e(rp.nom)} —</h3>")
        for c in sorted(rp.categories, key=lambda c: _rang(c.nom)):
            if c.plats:
                parts.append(f"<h3>{e(c.nom)}</h3><ul>" + "".join(f"<li>{e(p)}</li>" for p in c.plats) + "</ul>")
    parts.append("</section>")
    return "".join(parts)


async def _jours_disponibles() -> list[date]:
    jours: set[date] = set()
    for r in RESTAURANTS.values():
        for m in await menus.get_semaine(r):
            jours.add(m.jour)
    return sorted(jours)[:10]


async def page(request: web.Request) -> web.Response:
    from .bot import maintenant

    jours = await _jours_disponibles()
    demande = request.query.get("jour")
    try:
        jour = date.fromisoformat(demande) if demande else (jours[0] if jours else maintenant().date())
    except ValueError:
        raise web.HTTPBadRequest(text="jour=AAAA-MM-JJ")
    tabs = "".join(
        f'<a href="/?jour={d.isoformat()}" class="{"on" if d == jour else ""}">{jour_fr(d).capitalize()}</a>'
        for d in jours
    )
    cards = []
    for r in RESTAURANTS.values():
        try:
            m = await menus.get_menu(r, jour)
        except Exception:
            m = None
        cards.append(_card(m, r.nom, r.crous_url))
    return web.Response(text=PAGE.format(tabs=tabs, cards="".join(cards)), content_type="text/html",
                        headers={"Cache-Control": "public, max-age=300"})


async def api_menu(request: web.Request) -> web.Response:
    from .bot import maintenant

    try:
        jour = date.fromisoformat(request.query.get("jour", maintenant().date().isoformat()))
    except ValueError:
        raise web.HTTPBadRequest(text="jour=AAAA-MM-JJ")
    out = {"jour": jour.isoformat(), "restaurants": {}}
    for r in RESTAURANTS.values():
        try:
            m = await menus.get_menu(r, jour)
        except Exception as exc:
            out["restaurants"][r.key] = {"nom": r.nom, "erreur": str(exc)}
            continue
        out["restaurants"][r.key] = {
            "nom": r.nom, "source": m.source if m else None,
            "repas": [{"nom": rp.nom, "categories": [{"nom": c.nom, "plats": c.plats} for c in rp.categories]}
                      for rp in (m.repas if m else [])],
        }
    return web.json_response(out, dumps=lambda o: __import__("json").dumps(o, ensure_ascii=False))


async def start_web(bot: "MenuCrous") -> None:
    async def health(_request: web.Request) -> web.Response:
        ok = bot.is_ready() and not bot.is_closed()
        return web.json_response(
            {"discord": "ok" if ok else "deconnecte", "latence_ms": round(bot.latency * 1000) if ok else None,
             "uptime_s": int(time.time() - bot.demarrage), "serveurs": len(bot.guilds)},
            status=200 if ok or time.time() - bot.demarrage < 120 else 503,
        )

    app = web.Application()
    app.add_routes([web.get("/", page), web.get("/api/menu", api_menu), web.get("/health", health)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", bot.settings.web_port).start()
    log.info("Page web sur le port %d", bot.settings.web_port)
