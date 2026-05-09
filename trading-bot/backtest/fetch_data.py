"""
fetch_data.py – Download historical OHLCV data for backtesting.

Uses the BloFin public API (no auth required) to fetch historical
candlestick data and saves them to data/<SYMBOL>_15m.csv.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

BLOFIN_BASE_URL = "https://openapi.blofin.com"


def fetch_blofin_ohlcv(
    symbol: str = "BTC-USDT",
    bar: str = "15m",
    days: int = 365,
) -> pd.DataFrame:
    """
    Download up to *days* days of OHLCV data from the BloFin public API.

    Parameters
    ----------
    symbol : str
        BloFin instrument ID (e.g. ``"BTC-USDT"``).
    bar : str
        Candlestick interval (e.g. ``"15m"``).
    days : int
        Number of days of history to fetch.

    Returns a DataFrame indexed by ``datetime`` (UTC) with columns:
    open, high, low, close, volume.
    """
    url = f"{BLOFIN_BASE_URL}/api/v1/market/history-candles"
    limit = 100  # BloFin max per request

    # Target time range
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    all_candles: list[list] = []

    # BloFin history-candles returns newest-first and paginates backwards
    # using the ``before`` parameter (return candles older than this ts).
    current_before = end_ms
    while current_before > start_ms:
        params: dict = {
            "instId": symbol,
            "bar": bar,
            "limit": limit,
            "before": str(current_before),
            "after": str(start_ms),
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            break

        all_candles.extend(data)

        # BloFin returns newest-first; the oldest candle is the last element.
        oldest_ts = int(data[-1][0])
        if oldest_ts >= current_before:
            # No progress – avoid infinite loop
            break
        current_before = oldest_ts

        if len(data) < limit:
            break
        time.sleep(0.1)  # be polite

    if not all_candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # BloFin candle format: [ts, open, high, low, close, vol, volCcy, ...]
    # Keep only the first 6 columns we need.
    df = pd.DataFrame(all_candles)
    df = df.iloc[:, :6].copy()
    df.columns = ["ts", "open", "high", "low", "close", "volume"]
    for col in ["ts", "open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Deduplicate by timestamp (overlapping pages may return the same candle)
    df = df.drop_duplicates(subset="ts")

    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("datetime").sort_index()
    # Drop the raw ts column – no longer needed after indexing
    df = df.drop(columns=["ts"], errors="ignore")
    return df


def load_or_fetch(symbol: str = "BTC-USDT", days: int = 365) -> pd.DataFrame:
    """Load cached CSV or fetch fresh data from BloFin.

    Parameters
    ----------
    symbol : str
        BloFin instrument ID (e.g. ``"BTC-USDT"``).
    days : int
        Number of days of history to fetch when no cache exists.
    """
    csv_path = DATA_DIR / f"{symbol}_15m.csv"
    if csv_path.exists():
        print(f"Loading cached data from {csv_path}")
        df = pd.read_csv(csv_path, index_col="datetime", parse_dates=True)
        return df
    print(f"Fetching {days} days of {symbol} 15m data from BloFin …")
    df = fetch_blofin_ohlcv(symbol, "15m", days)
    df.to_csv(csv_path)
    print(f"Saved {len(df)} candles to {csv_path}")
    return df


if __name__ == "__main__":
    df = load_or_fetch("BTC-USDT", days=365)
    print(df.tail())
    print(f"\nTotal candles: {len(df)}")
    print(f"Date range: {df.index[0]} – {df.index[-1]}")
