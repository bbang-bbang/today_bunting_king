"""오늘자 추천 강제 최신화 + 텔레그램 푸시.

- 액션(매수·매도·건너뜀) 보유 rec_id 는 보존 (회고 추적용)
- 액션 없는 옛 추천은 삭제 → 신규 추천이 cache canonical 이 됨
- recommend() 직접 호출 후 send_recommendations_dual 트리거
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date

from telegram import Bot

from src import config
from src.bot.scheduler import _list_candidate_codes, _get_approved_users, send_recommendations_dual
from src.db.connection import get_connection


def cleanup_unactioned_today() -> int:
    """오늘자 액션 없는 추천 삭제. 삭제된 건수 반환."""
    today = date.today().isoformat()
    conn = get_connection()
    try:
        cur = conn.execute(
            """DELETE FROM recommendations
               WHERE session_date = ?
                 AND rec_id NOT IN (SELECT rec_id FROM recommendation_actions)""",
            (today,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


async def main() -> int:
    deleted = cleanup_unactioned_today()
    print(f"액션 없는 옛 추천 {deleted}건 삭제")

    codes = _list_candidate_codes()
    users = _get_approved_users()
    if not codes or not users:
        print(f"ERROR codes={len(codes)} users={len(users)}")
        return 1

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    async with bot:
        for u in users:
            n = await send_recommendations_dual(bot, u.chat_id, codes, force_fresh=True)
            print(f"chat_id={u.chat_id} sent={n}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
