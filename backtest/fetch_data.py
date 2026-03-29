"""
fetch_data.py – Download historical OHLCV data for backtesting.

Uses Binance public API (no auth required) to fetch BTC/USDT 15-minute
candles for the past 12 months and saves them to data/BTCUSDT_15m.csv.

BloFin does not require API keys for historical market data via their
public endpoints, but Binance provides a generous free history endpoint
that is widely used for backtesting purposes.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def fetch_binance_ohlcv(
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    days: int = 365,
) -> pd.DataFrame:
    """
    Download up to `days` days of OHLCV data from Binance public API.
    Returns a DataFrame with columns: datetime, open, high, low, close, volume.
    """
    url = "https://api.binance.com/api/v3/klines"
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    all_candles: list[list] = []
    limit = 1000  # Binance max per request

    current_start = start_ms
    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": limit,
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        candles = resp.json()
        if not candles:
            break
        all_candles.extend(candles)
        current_start = candles[-1][0] + 1  # next ms after last candle
        if len(candles) < limit:
            break
        time.sleep(0.1)  # be polite

    df = pd.DataFrame(
        all_candles,
        columns=[
            "ts", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ],
    )
    df = df[["ts", "open", "high", "low", "close", "volume"]].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("datetime").sort_index()
    return df


def load_or_fetch(symbol: str = "BTCUSDT", days: int = 365) -> pd.DataFrame:
    """Load cached CSV or fetch fresh data."""
    csv_path = DATA_DIR / f"{symbol}_15m.csv"
    if csv_path.exists():
        print(f"Loading cached data from {csv_path}")
        df = pd.read_csv(csv_path, index_col="datetime", parse_dates=True)
        return df
    print(f"Fetching {days} days of {symbol} 15m data from Binance …")
    df = fetch_binance_ohlcv(symbol, "15m", days)
    df.to_csv(csv_path)
    print(f"Saved {len(df)} candles to {csv_path}")
    return df


if __name__ == "__main__":
    df = load_or_fetch("BTCUSDT", days=365)
    print(df.tail())
    print(f"\nTotal candles: {len(df)}")
    print(f"Date range: {df.index[0]} – {df.index[-1]}")
