"""DB에서 OHLCV DataFrame 로드."""
from __future__ import annotations

from datetime import date

import pandas as pd

from src.db.connection import get_connection


def load_ohlcv(
    code: str,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """종목 OHLCV 를 DatetimeIndex 의 DataFrame 으로 반환.

    컬럼: open, high, low, close, volume, value, change_pct
    빈 결과면 컬럼 없는 빈 DataFrame 반환.
    """
    conn = get_connection()
    try:
        q = "SELECT date, open, high, low, close, volume, value, change_pct FROM ohlcv_daily WHERE code = ?"
        params: list = [code]
        if start:
            q += " AND date >= ?"
            params.append(start.isoformat())
        if end:
            q += " AND date <= ?"
            params.append(end.isoformat())
        q += " ORDER BY date"
        df = pd.read_sql_query(q, conn, params=params, parse_dates=["date"])
        if df.empty:
            return df
        return df.set_index("date")
    finally:
        conn.close()
