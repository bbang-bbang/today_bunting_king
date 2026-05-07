"""오늘 추천을 즉시 텔레그램 채널로 푸시 (DB 캐시 + 매수 버튼)."""
from __future__ import annotations
import asyncio
import os
import sys

from telegram import Bot

from src import config
from src.bot.scheduler import _get_approved_users, _list_candidate_codes, send_recommendations_dual


async def main() -> int:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN 미설정", file=sys.stderr)
        return 1

    codes = _list_candidate_codes()
    users = _get_approved_users()
    if not codes:
        print("ERROR: analysis_universe 비어있음", file=sys.stderr)
        return 1
    if not users:
        print("ERROR: 승인된 사용자 없음", file=sys.stderr)
        return 1

    print(f"universe: {len(codes)}개, 사용자: {len(users)}명, seed: {config.SEED_KRW:,}")
    bot = Bot(token=token)
    async with bot:
        for u in users:
            n = await send_recommendations_dual(bot, u.chat_id, codes)
            print(f"chat_id={u.chat_id} sent={n}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
