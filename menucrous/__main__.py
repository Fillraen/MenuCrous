"""python -m menucrous"""
import logging

from .bot import MenuCrous
from .config import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    settings = load_settings()
    MenuCrous(settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
