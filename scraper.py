from playwright.async_api import async_playwright
from bs4 import BeautifulSoup, NavigableString

URL = "https://www.crous-lyon.fr/restaurant/restaurant-manufacture-des-tabacs/"


# Catégories masquées en mode normal (affichées seulement en mode complet)
_CATEGORIES_COMPLET_ONLY = {"entrée", "dessert"}


async def get_todays_menu(complet: bool = False) -> str:
    """Récupère et formate le menu du jour (Manufacture des Tabacs, Lyon 3).

    Args:
        complet: Si True, affiche toutes les catégories (entrées, desserts compris).
    """
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
                await page.wait_for_selector(".slick-current", timeout=10000)
            except Exception:
                pass  # On continue même si le slider n'est pas encore initialisé
            html = await page.content()
            await browser.close()
    except Exception as exc:
        return f"❌ Impossible de récupérer le menu : {exc}"

    return _parse_menu(html, complet=complet)


def _parse_menu(html: str, complet: bool = False) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # Le slide actif (.slick-current) correspond au menu du jour
    slide = soup.find("div", class_="slick-current")
    if not slide:
        # Fallback : premier slide disponible
        slide = soup.find("div", class_="slick-slide")
    if not slide:
        return "📭 Aucun menu disponible aujourd'hui (restaurant fermé ou page indisponible)."

    lines: list[str] = []

    # Date du menu
    date_elem = slide.find("time", class_="menu_date_title")
    if date_elem:
        lines.append(f"🍽️ **{date_elem.get_text(strip=True)}**")
        lines.append("━" * 38)

    # Parcours des repas (Déjeuner, Dîner…)
    for meal in slide.find_all("div", class_="meal"):
        title_elem = meal.find("div", class_="meal_title")
        if title_elem:
            lines.append(f"\n**— {title_elem.get_text(strip=True).upper()} —**")

        foodies = meal.find("ul", class_="meal_foodies")
        if not foodies:
            continue

        for cat_li in foodies.find_all("li", recursive=False):
            # Le nom de catégorie est le texte direct du <li> (pas dans un tag enfant)
            cat_name = "".join(
                str(child) for child in cat_li.children
                if isinstance(child, NavigableString)
            ).strip()

            # Filtrage selon le mode
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
    return "\n".join(lines) if lines else "📭 Aucun menu disponible aujourd'hui."
