"""
Telegram bot runner.

Usage:
    BOOMER_DB_PATH=data/boomer.db \
    TELEGRAM_BOT_TOKEN=<token> \
    TELEGRAM_CHAT_ID=<chat_id> \
    python scripts/run_telegram_bot.py
"""

import logging
import sys
from pathlib import Path

# Allow imports from src/ before project is installed as a package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from alerts.telegram_bot import from_env  # noqa: E402

bot = from_env()
bot.run_forever()
