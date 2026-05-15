"""Market-data layer for the IV collector.

Alpaca clients, daily-bar fetch, universe loaders, and IV-cache I/O —
extracted verbatim from the private bot's `skills/options_screen.py`.
This is the only non-generic module here, and it deliberately holds
*no* strategy or scoring logic — just the plumbing the IV backfill
needs. IV history is public market data; nothing private lives here.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from clock import today_et

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
IV_CACHE_DIR = ROOT / "iv"
UNIVERSE_DIR = ROOT / "universe"
CURATED_UNIVERSE_PATH = UNIVERSE_DIR / "curated_universe.md"
MANUAL_UNIVERSE_PATH = UNIVERSE_DIR / "manual_universe.md"


def get_alpaca_client() -> StockHistoricalDataClient:
    """Build the Alpaca stock historical-data client from env vars."""
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY must be set")
    return StockHistoricalDataClient(key, secret)


def get_options_client() -> OptionHistoricalDataClient:
    """Build the Alpaca options historical-data client from env vars."""
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY must be set")
    return OptionHistoricalDataClient(key, secret)


def _to_yfinance_symbol(ticker: str) -> str:
    """Translate Alpaca-style symbols to yfinance form (BRK.B -> BRK-B)."""
    return ticker.replace(".", "-")


def fetch_daily_bars(
    ticker: str,
    client: StockHistoricalDataClient,
    days: int = 365,
) -> pd.DataFrame:
    """Daily bars for `ticker` over the past `days`.

    Returns a DataFrame indexed by timestamp with at least a `close`
    column. Uses Alpaca's free IEX feed; falls back to yfinance if
    Alpaca returns nothing.
    """
    end = today_et()
    start = end - timedelta(days=days)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        df = client.get_stock_bars(req).df
        if df.empty:
            raise ValueError("empty Alpaca response")
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(ticker, level=0)
        return df
    except Exception as exc:
        logger.warning(
            "Alpaca bars failed for %s (%s); falling back to yfinance",
            ticker,
            exc,
        )
        df = yf.Ticker(_to_yfinance_symbol(ticker)).history(
            period=f"{days}d", auto_adjust=True
        )
        df.columns = [c.lower() for c in df.columns]
        return df


def _load_ticker_file(path: Path) -> list[str]:
    """Parse a ticker file: one ticker per line; `#` comments and blanks ignored."""
    if not path.exists():
        return []
    tickers: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tickers.append(line.upper())
    return tickers


def load_curated_universe(path: Path = CURATED_UNIVERSE_PATH) -> list[str]:
    """Curated-universe tickers (uppercased)."""
    return _load_ticker_file(path)


def load_manual_universe(path: Path = MANUAL_UNIVERSE_PATH) -> list[str]:
    """Manual-universe tickers (uppercased)."""
    return _load_ticker_file(path)


def load_iv_history_dated(
    ticker: str, *, cache_dir: Path = IV_CACHE_DIR
) -> pd.DataFrame:
    """Cached IV history as a `(date, iv)` DataFrame, newest-first.

    Empty `date`/`iv` frame when the cache file is absent. Used by the
    backfill's date-aware skip filter (`skip_if_reaches`).
    """
    cache_path = cache_dir / f"{ticker}.parquet"
    if not cache_path.exists():
        return pd.DataFrame(columns=["date", "iv"])
    df = pd.read_parquet(cache_path)
    df["iv"] = df["iv"].astype(float)
    return df.sort_values("date", ascending=False).reset_index(drop=True)
