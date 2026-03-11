import subprocess
import sys

subprocess.run(
    [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
    check=True,
)

from bot import bot, TOKEN

bot.run(TOKEN)
