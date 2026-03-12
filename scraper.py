from datetime import date, timedelta

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup, NavigableString

URL = "https://www.crous-lyon.fr/restaurant/restaurant-manufacture-des-tabacs/"

_CATEGORIES_COMPLET_ONLY = {"entrée", "dessert"}

_FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}


async def get_menu(date_offset: int = 0, complet: bool = False) -> str:
    """Récupère le menu pour le jour cible (date_offset=0 → aujourd'hui, 1 → demain…)."""
    target = date.today() + timedelta(days=date_offset)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()
            await page.goto(URL, wait_until="networkidle", timeout=30000)
            try:
                await page.wait_for_selector(".slick-slide", timeout=10000)
            except Exception:
                pass
            html = await page.content()
            await browser.close()
    except Exception as exc:
        return f"❌ Impossible de récupérer le menu : {exc}"

    return _parse_menu_for_date(html, target, complet=complet)


def _parse_slide_date(text: str) -> date | None:
    """Extrait une date depuis 'Menu du mercredi 11 mars 2026'."""
    parts = text.lower().split()
    for i, part in enumerate(parts):
        if part.isdigit() and i + 2 < len(parts):
            try:
                day = int(part)
                month = _FRENCH_MONTHS.get(parts[i + 1])
                year = int(parts[i + 2])
                if month:
                    return date(year, month, day)
            except (ValueError, IndexError):
                continue
    return None


def _parse_menu_for_date(html: str, target: date, complet: bool = False) -> str:
    soup = BeautifulSoup(html, "html.parser")

    target_slide = None
    for slide in soup.find_all("div", class_="slick-slide"):
        time_elem = slide.find("time", class_="menu_date_title")
        if time_elem and _parse_slide_date(time_elem.get_text()) == target:
            target_slide = slide
            break

    if not target_slide:
        _DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        day_name = _DAYS_FR[target.weekday()]
        return (
            f"📭 Pas de menu disponible pour le **{day_name} {target.strftime('%d/%m/%Y')}** "
            f"(restaurant fermé ou hors de la semaine en cours)."
        )

    return _parse_slide(target_slide, complet=complet)


def _parse_slide(slide, complet: bool = False) -> str:
    lines: list[str] = []

    date_elem = slide.find("time", class_="menu_date_title")
    if date_elem:
        lines.append(f"🍽️ **{date_elem.get_text(strip=True)}**")
        lines.append("━" * 38)

    for meal in slide.find_all("div", class_="meal"):
        title_elem = meal.find("div", class_="meal_title")
        if title_elem:
            lines.append(f"\n**— {title_elem.get_text(strip=True).upper()} —**")

        foodies = meal.find("ul", class_="meal_foodies")
        if not foodies:
            continue

        for cat_li in foodies.find_all("li", recursive=False):
            cat_name = "".join(
                str(child) for child in cat_li.children
                if isinstance(child, NavigableString)
            ).strip()

            if not complet and any(
                cat_name.lower().startswith(excl) for excl in _CATEGORIES_COMPLET_ONLY
            ):
                continue

            if cat_name:
                lines.append(f"\n**{cat_name}**")

            sub_ul = cat_li.find("ul")
            if sub_ul:
                for item in sub_ul.find_all("li"):
                    lines.append(f"  • {item.get_text(strip=True)}")

    if not complet and lines:
        lines.append(
            "\n> ℹ️ Entrées et desserts masqués. Utilise `/menu mode:complet` pour tout voir."
        )
    return "\n".join(lines) if lines else "📭 Aucun menu disponible."
