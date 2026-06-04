import asyncio
from unittest.mock import AsyncMock, patch
from src import config
from src.bot import scheduler


def test_regime_failure_alerts_admin():
    bot = AsyncMock()
    with patch.object(config, "MARKET_DOWN_THRESHOLD_PCT", -1.5), \
         patch.object(config, "TELEGRAM_ADMIN_CHAT_ID", 999), \
         patch("pykrx.stock.get_index_ohlcv", side_effect=RuntimeError("empty")):
        ok, reason = asyncio.run(scheduler._check_market_regime(bot))
    assert ok is True and reason == ""          # 추천은 계속 진행
    bot.send_message.assert_awaited_once()        # 관리자 경보 1회
    assert "레짐 체크 실패" in bot.send_message.await_args.args[1]


def test_regime_failure_no_bot_no_crash():
    with patch.object(config, "MARKET_DOWN_THRESHOLD_PCT", -1.5), \
         patch("pykrx.stock.get_index_ohlcv", side_effect=RuntimeError("empty")):
        ok, reason = asyncio.run(scheduler._check_market_regime(None))
    assert ok is True and reason == ""            # bot 없으면 조용히 통과
