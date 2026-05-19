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
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import ContractType

from alpaca_resilience import apply_session_timeout, call_with_retry
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
    return apply_session_timeout(StockHistoricalDataClient(key, secret))


def get_options_client() -> OptionHistoricalDataClient:
    """Build the Alpaca options historical-data client from env vars."""
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY must be set")
    return apply_session_timeout(OptionHistoricalDataClient(key, secret))


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
        df = call_with_retry(
            lambda: client.get_stock_bars(req), what=f"stock bars {ticker}"
        ).df
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


def _append_iv(
    ticker: str, day: date, iv: float, cache_dir: Path = IV_CACHE_DIR
) -> None:
    """Append (or update) `day`'s IV in the cache parquet for `ticker`.

    Idempotent — if a row already exists for `day`, it's replaced.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{ticker}.parquet"
    ts = pd.Timestamp(day).normalize()
    new_row = pd.DataFrame({"date": [ts], "iv": [float(iv)]})
    if cache_path.exists():
        existing = pd.read_parquet(cache_path)
        existing = existing[existing["date"] != ts]
        combined = pd.concat([existing, new_row], ignore_index=True)
    else:
        combined = new_row
    combined = combined.sort_values("date", ascending=False).reset_index(drop=True)
    combined.to_parquet(cache_path)


def update_iv_today(
    ticker: str,
    client: OptionHistoricalDataClient,
    *,
    today: date | None = None,
    target_dte: int = 30,
    dte_tolerance: int = 7,
    cache_dir: Path = IV_CACHE_DIR,
) -> tuple[float | None, dict]:
    """Fetch today's ATM put IV for `ticker` and append to cache.

    "ATM put" = the put contract whose `|delta|` is closest to 0.50.
    "~30 DTE" = expiration within `target_dte ± dte_tolerance` days
    (default 23–37 days, i.e. 30 ± 7).

    Reads IV straight off the live option-chain snapshot — no
    Black-Scholes back-solve. This is what pins a *fresh* endpoint on
    each cache; the backfill only reconstructs history (and lags 1-3
    days because recent option bars aren't available yet).

    Side effect: appends a row to `iv/<ticker>.parquet` when an ATM put
    is found. Idempotent — re-running on the same day replaces today's
    row rather than duplicating it.

    Returns `(iv, meta)`. `iv` is None if no qualifying contract was
    found (e.g., chain empty, no greeks, no IV populated).
    """
    today = today or today_et()

    req = OptionChainRequest(
        underlying_symbol=ticker,
        type=ContractType.PUT,
        expiration_date_gte=today + timedelta(days=target_dte - dte_tolerance),
        expiration_date_lte=today + timedelta(days=target_dte + dte_tolerance),
    )
    chain = call_with_retry(
        lambda: client.get_option_chain(req), what=f"option chain {ticker}"
    )

    best_iv: float | None = None
    best_distance = float("inf")
    best_symbol: str | None = None
    for symbol, snap in chain.items():
        if not snap.greeks or snap.implied_volatility is None:
            continue
        distance = abs(abs(snap.greeks.delta) - 0.50)
        if distance < best_distance:
            best_distance = distance
            best_iv = float(snap.implied_volatility)
            best_symbol = symbol

    if best_iv is not None:
        _append_iv(ticker, today, best_iv, cache_dir=cache_dir)

    return best_iv, {
        "ticker": ticker,
        "date": today,
        "iv": best_iv,
        "atm_symbol": best_symbol,
        "delta_distance_from_atm": (
            best_distance if best_distance != float("inf") else None
        ),
        "chain_size": len(chain),
    }
