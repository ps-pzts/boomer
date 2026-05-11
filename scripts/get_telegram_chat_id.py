"""Get your Telegram chat ID after sending a message to your bot.

Usage:
    1. Add TELEGRAM_BOT_TOKEN to .env
    2. Open your bot in Telegram and send it any message (e.g. "hello")
    3. Run: PYTHONPATH=src .venv/bin/python scripts/get_telegram_chat_id.py
    4. Copy the chat_id and add it to .env as TELEGRAM_CHAT_ID=
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

_env_path = Path(__file__).parents[1] / ".env"
if _env_path.exists():
    from dotenv import load_dotenv

    load_dotenv(_env_path, override=True)

token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not token:
    print("ERROR: Set TELEGRAM_BOT_TOKEN in your .env file first.")
    sys.exit(1)

url = f"https://api.telegram.org/bot{token}/getUpdates"
try:
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
except Exception as exc:
    print(f"ERROR: Could not reach Telegram API — {exc}")
    sys.exit(1)

if not data.get("ok"):
    print(f"ERROR: Telegram API returned error — {data}")
    sys.exit(1)

updates = data.get("result", [])
if not updates:
    print("No messages found. Send any message to your bot in Telegram first, then rerun.")
    sys.exit(1)

seen = {}
for update in updates:
    msg = update.get("message") or update.get("channel_post") or {}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "?")
    name = chat.get("title") or chat.get("first_name") or chat.get("username") or "?"
    if chat_id and chat_id not in seen:
        seen[chat_id] = (chat_type, name)

if not seen:
    print("No chat IDs found in recent updates. Send a message to your bot and retry.")
    sys.exit(1)

print("\n── Found chat IDs ──────────────────────────────────────")
for chat_id, (chat_type, name) in seen.items():
    print(f"  chat_id : {chat_id}")
    print(f"  type    : {chat_type}")
    print(f"  name    : {name}")
    print()

if len(seen) == 1:
    chat_id = list(seen.keys())[0]
    print("Add this to your .env:")
    print(f"  TELEGRAM_CHAT_ID={chat_id}")
else:
    print("Multiple chats found — pick the one you want and add to .env:")
    print("  TELEGRAM_CHAT_ID=<chat_id>")
print("────────────────────────────────────────────────────────\n")
