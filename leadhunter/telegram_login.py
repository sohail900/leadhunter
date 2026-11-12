import asyncio
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from leadhunter.config import get_settings


async def main() -> int:
    settings = get_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first (my.telegram.org).")
        return 1
    client = TelegramClient(StringSession(), settings.telegram_api_id, settings.telegram_api_hash)
    await client.start()
    print("\nAdd this line to .env:\n")
    print(f"TELEGRAM_SESSION={client.session.save()}")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
