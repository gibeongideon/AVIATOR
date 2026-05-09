"""
Local runner — no server needed.
All strategy logic lives in src/bot.py (single source of truth).
Run:  python bot.py
"""
import asyncio
from src.bot import AviatorBot


async def main():
    await AviatorBot().run()


if __name__ == "__main__":
    asyncio.run(main())
