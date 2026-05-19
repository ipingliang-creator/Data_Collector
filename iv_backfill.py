"""IV backfill — populate historical IV per ticker from Alpaca data.

The daily `update_iv_today` only stores one IV per call (the ATM
put at ~30 DTE today). For the IV-Rank gate in CSP entry to be
meaningful we need ~252 *trading* days of history (≈ one calendar
year), which would otherwise take ~12 months of natural
accumulation. This module reconstructs that history offline.

Algorithm per ticker (`backfill_one_ticker`):
1. Fetch stock daily bars over the lookback window — gives us
   the trading-day calendar and the underlying close on each day.
2. Enumerate Alpaca put contracts with expiry in the window
   `[start + 23, today + 37]` (active + inactive — expired
   contracts are needed for older dates).
3. For each trading day D in the window:
   a. Pick the contract whose expiry was 23–37 days from D and
      whose strike is closest to that day's close.
   b. Fetch its bar (close) on D.
   c. Compute T = `(expiry - D)/365`, spot = close on D,
      K = strike, market_price = option close on D.
   d. Back-solve IV via Black-Scholes.
4. Merge results into `state/cache/iv/<ticker>.parquet`.

Each `backfill_one_ticker` run prints a one-line diagnostic with
the count of days computed vs. the per-reason drop breakdown
(`no_contract` / `no_bar` / `bar_error` / `solver_fail`) — useful
for spotting why a ticker's cache is thin.

Constants:
- `RISK_FREE_RATE = 0.045` — flat ~current US 1-month T-bill.
  Replace with a daily curve once a source is wired up.
- `TARGET_DTE = 30`, `DTE_WINDOW = (23, 37)` — match `update_iv_today`.
- `LOOKBACK_CALENDAR_DAYS = 400` — calendar-day reach of the
  backfill. `compute_ivr` ranks against a trailing 365-calendar-day
  window; 400 days of backfill gives that span margin so it is
  always fully covered. (The earlier value of 252 was a bug — 252
  *calendar* days is only ~8.3 months, which truncated every cache
  to a Sep–May window.)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from alpaca_resilience import apply_session_timeout, call_with_retry
from black_scholes import solve_put_iv
from clock import today_et
from market_data import IV_CACHE_DIR, fetch_daily_bars

logger = logging.getLogger(__name__)

RISK_FREE_RATE = 0.045
TARGET_DTE = 30
DTE_WINDOW = (23, 37)
# Calendar-day reach of the backfill. 252 *trading* days ≈ 1 calendar
# year; 400 calendar days (~275 trading days) covers `compute_ivr`'s
# trailing-252 window with margin. NOT 252 — that is a trading-day
# count and as calendar days spans only ~8.3 months.
LOOKBACK_CALENDAR_DAYS = 400


def get_trading_client() -> TradingClient:
    """Build the Alpaca trading client (for option-contract enumeration)."""
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY must be set"
        )
    return apply_session_timeout(TradingClient(key, secret, paper=True))


def list_put_contracts(
    ticker: str,
    trading_client: TradingClient,
    *,
    expiration_date_gte: date,
    expiration_date_lte: date,
) -> list[Any]:
    """Enumerate put contracts whose expiry falls in the date range.

    Queries both `ACTIVE` and `INACTIVE` statuses (the latter includes
    expired contracts — required for historical days). Paginates
    automatically.
    """
    all_contracts: list[Any] = []
    for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
        page_token: str | None = None
        while True:
            req = GetOptionContractsRequest(
                underlying_symbols=[ticker],
                expiration_date_gte=expiration_date_gte,
                expiration_date_lte=expiration_date_lte,
                type=ContractType.PUT,
                status=status,
                limit=1000,
                page_token=page_token,
            )
            resp = call_with_retry(
                lambda: trading_client.get_option_contracts(req),
                what=f"contracts {ticker}",
            )
            contracts = getattr(resp, "option_contracts", None) or []
            all_contracts.extend(contracts)
            page_token = getattr(resp, "next_page_token", None)
            if not page_token:
                break
    return all_contracts


def pick_contract_for_date(
    contracts: list[Any], target_date: date, target_close: float
) -> Any | None:
    """Pick the put contract closest to `TARGET_DTE` from `target_date`
    with strike nearest `target_close`.

    Returns `None` if no contract has DTE in `DTE_WINDOW` relative to
    `target_date`. Tie-break is `(|strike - close|, |DTE - 30|)`.
    """
    eligible = []
    for c in contracts:
        # `expiration_date` may be a date or a string per SDK version.
        exp = c.expiration_date
        if isinstance(exp, str):
            exp = date.fromisoformat(exp)
        dte = (exp - target_date).days
        if DTE_WINDOW[0] <= dte <= DTE_WINDOW[1]:
            eligible.append((c, exp, dte))
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda t: (
            abs(float(t[0].strike_price) - target_close),
            abs(t[2] - TARGET_DTE),
        ),
    )[0]


def write_iv_cache(
    ticker: str,
    ivs: dict[date, float],
    *,
    cache_dir: Path = IV_CACHE_DIR,
) -> None:
    """Merge new IV entries into `state/cache/iv/<ticker>.parquet`.

    Existing entries are preserved; overlapping dates are overwritten
    by the new value (last-wins). Format matches `load_iv_history`:
    columns `date` (Timestamp), `iv` (float).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{ticker}.parquet"
    new_df = pd.DataFrame({
        "date": [pd.Timestamp(d) for d in sorted(ivs.keys())],
        "iv": [ivs[d] for d in sorted(ivs.keys())],
    })
    if path.exists():
        existing = pd.read_parquet(path)
        combined = (
            pd.concat([existing, new_df])
            .drop_duplicates(subset="date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
    else:
        combined = new_df.reset_index(drop=True)
    combined.to_parquet(path, index=False)


def backfill_one_ticker(
    ticker: str,
    *,
    lookback_days: int = LOOKBACK_CALENDAR_DAYS,
    today: date | None = None,
    trading_client: TradingClient | None = None,
    options_client: OptionHistoricalDataClient | None = None,
    stock_client: StockHistoricalDataClient | None = None,
    cache_dir: Path = IV_CACHE_DIR,
    risk_free_rate: float = RISK_FREE_RATE,
) -> dict[date, float]:
    """Backfill historical IV for one ticker.

    `lookback_days` is a **calendar**-day reach (default 400 ≈ one
    year + margin), not a trading-day count.

    Returns `{date: iv}` for every day we could compute. Writes to
    `cache_dir / <ticker>.parquet` if non-empty (merges with any
    existing entries).

    Failures per individual day (missing bar, IV solver returns None,
    etc.) are logged at DEBUG and skipped — one bad day doesn't kill
    the run. A one-line summary of days computed vs. per-reason drops
    is printed at the end for density diagnostics.
    """
    today = today or today_et()
    start_date = today - timedelta(days=lookback_days)

    # Lazy client construction so tests can pass mocks.
    if trading_client is None:
        trading_client = get_trading_client()
    if options_client is None:
        from market_data import get_options_client
        options_client = get_options_client()
    if stock_client is None:
        from market_data import get_alpaca_client
        stock_client = get_alpaca_client()

    bars_df = fetch_daily_bars(ticker, stock_client, days=lookback_days + 30)
    if bars_df is None or bars_df.empty:
        logger.warning("No stock bars for %s; skipping backfill", ticker)
        return {}

    contracts = list_put_contracts(
        ticker, trading_client,
        expiration_date_gte=start_date + timedelta(days=DTE_WINDOW[0]),
        expiration_date_lte=today + timedelta(days=DTE_WINDOW[1]),
    )
    if not contracts:
        logger.warning("No put contracts found for %s; skipping backfill", ticker)
        return {}

    ivs: dict[date, float] = {}
    drops = {"no_contract": 0, "no_bar": 0, "bar_error": 0, "solver_fail": 0}
    days_seen = 0
    for ts, row in bars_df.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        if d < start_date or d > today:
            continue
        days_seen += 1
        spot = float(row["close"])
        contract = pick_contract_for_date(contracts, d, spot)
        if contract is None:
            drops["no_contract"] += 1
            continue

        bar_req = OptionBarsRequest(
            symbol_or_symbols=contract.symbol,
            timeframe=TimeFrame.Day,
            start=d,
            end=d + timedelta(days=1),
        )
        try:
            opt_bars = call_with_retry(
                lambda: options_client.get_option_bars(bar_req),
                what=f"option bars {contract.symbol}",
            )
            opt_df = opt_bars.df
        except Exception as exc:
            logger.debug("Bar fetch failed for %s on %s: %s",
                         contract.symbol, d, exc)
            drops["bar_error"] += 1
            continue
        if opt_df is None or opt_df.empty:
            drops["no_bar"] += 1
            continue

        close = float(opt_df.iloc[0]["close"])
        K = float(contract.strike_price)
        exp = contract.expiration_date
        if isinstance(exp, str):
            exp = date.fromisoformat(exp)
        T = (exp - d).days / 365.0
        iv = solve_put_iv(spot, K, T, risk_free_rate, close)
        if iv is not None:
            ivs[d] = iv
        else:
            drops["solver_fail"] += 1

    print(
        f"  {ticker}: {len(ivs)}/{days_seen} days computed · drops: "
        f"no_contract={drops['no_contract']} no_bar={drops['no_bar']} "
        f"bar_error={drops['bar_error']} solver_fail={drops['solver_fail']}",
        flush=True,
    )

    if ivs:
        write_iv_cache(ticker, ivs, cache_dir=cache_dir)

    return ivs


def backfill_universe(
    tickers: list[str] | None = None,
    *,
    lookback_days: int = LOOKBACK_CALENDAR_DAYS,
    today: date | None = None,
    cache_dir: Path = IV_CACHE_DIR,
    skip_if_at_least: int | None = None,
    skip_if_reaches: date | None = None,
    max_seconds: float | None = None,
) -> dict[str, int]:
    """Run `backfill_one_ticker` across the whole universe.

    Builds the Alpaca clients once and reuses them across tickers
    (single TradingClient, OptionHistoricalDataClient, StockHistoricalDataClient).
    Per-ticker failures are caught and logged — one bad ticker doesn't
    abort the run.

    `tickers`: defaults to `load_curated_universe() ∪ load_manual_universe()`.

    Two independent skip filters speed up reruns (a ticker is skipped if
    *either* matches):

    - `skip_if_at_least`: skip any ticker whose cache already has at
      least this many entries.
    - `skip_if_reaches`: skip any ticker whose cache's *earliest* date
      is on or before this date. Use this to resume a partially-finished
      reprocess: row count alone can't tell a freshly reprocessed cache
      (deep history) from a stale one (same count, shallow history) —
      the earliest date can.

    `max_seconds`: if set, stop the loop after this many seconds even
    if tickers remain. Lets the CI job commit + push partial progress
    before hitting the runner's hard timeout. The "stopped" tickers
    appear in the result with count 0 and reason "TIME_LIMIT" so
    follow-up runs can pick them up via the skip filters.

    Returns `{ticker: count of IV entries written this run}`. Tickers
    skipped by a filter or by `max_seconds` are NOT in the result dict.
    """
    if tickers is None:
        from market_data import load_curated_universe, load_manual_universe
        tickers = sorted(
            set(load_curated_universe()) | set(load_manual_universe())
        )

    from market_data import (
        get_alpaca_client,
        get_options_client,
        load_iv_history_dated,
    )
    trading_client = get_trading_client()
    options_client = get_options_client()
    stock_client = get_alpaca_client()

    results: dict[str, int] = {}
    skipped = 0
    timed_out = 0
    start_time = time.monotonic()
    print(f"Backfilling {len(tickers)} tickers"
          + (f" (time budget: {max_seconds:.0f}s)" if max_seconds else "")
          + "...", flush=True)
    for i, ticker in enumerate(tickers, 1):
        if max_seconds is not None:
            elapsed = time.monotonic() - start_time
            if elapsed >= max_seconds:
                remaining = len(tickers) - (i - 1)
                print(
                    f"\n*** Time budget exhausted at {elapsed:.0f}s; "
                    f"stopping. {remaining} tickers remain unprocessed.",
                    flush=True,
                )
                timed_out = remaining
                break
        if skip_if_at_least is not None or skip_if_reaches is not None:
            cached = load_iv_history_dated(ticker, cache_dir=cache_dir)
            if skip_if_at_least is not None and len(cached) >= skip_if_at_least:
                print(f"  [{i}/{len(tickers)}] {ticker}: skip "
                      f"(cache has {len(cached)} entries ≥ {skip_if_at_least})",
                      flush=True)
                skipped += 1
                continue
            if (
                skip_if_reaches is not None
                and not cached.empty
                and cached["date"].min().date() <= skip_if_reaches
            ):
                earliest = cached["date"].min().date()
                print(f"  [{i}/{len(tickers)}] {ticker}: skip "
                      f"(cache reaches back to {earliest} ≤ {skip_if_reaches})",
                      flush=True)
                skipped += 1
                continue
        try:
            ivs = backfill_one_ticker(
                ticker,
                lookback_days=lookback_days,
                today=today,
                trading_client=trading_client,
                options_client=options_client,
                stock_client=stock_client,
                cache_dir=cache_dir,
            )
            print(f"  [{i}/{len(tickers)}] {ticker}: {len(ivs)} entries",
                  flush=True)
            results[ticker] = len(ivs)
        except Exception as exc:
            logger.warning("Backfill failed for %s: %s", ticker, exc)
            print(f"  [{i}/{len(tickers)}] {ticker}: ERROR {exc}", flush=True)
            results[ticker] = 0

    ok = sum(1 for v in results.values() if v > 0)
    none_written = sum(1 for v in results.values() if v == 0)
    print(
        f"\nSummary: {ok} written · {none_written} empty · "
        f"{skipped} skipped · {timed_out} not reached "
        f"(time={time.monotonic() - start_time:.0f}s)",
        flush=True,
    )
    return results


def main() -> int:
    """CLI: `python -m iv_backfill TICKER` for one ticker
    or `python -m iv_backfill --all` for the full universe.

    With `--all`, two optional env vars skip tickers on reruns:
    - `IV_BACKFILL_SKIP_IF_AT_LEAST=NN` — skip tickers whose cache
      already has >= NN entries.
    - `IV_BACKFILL_SKIP_IF_REACHES=YYYY-MM-DD` — skip tickers whose
      cache already reaches back to (on/before) that date. Use this
      to resume a partial reprocess without re-doing finished tickers.
    """
    import os
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        skip = os.environ.get("IV_BACKFILL_SKIP_IF_AT_LEAST")
        skip_val = int(skip) if skip else None
        reaches = os.environ.get("IV_BACKFILL_SKIP_IF_REACHES")
        skip_reaches = date.fromisoformat(reaches) if reaches else None
        max_s = os.environ.get("IV_BACKFILL_MAX_SECONDS")
        max_seconds = float(max_s) if max_s else None
        print(
            f"=== IV backfill for full universe "
            f"(skip_if_at_least={skip_val}, skip_if_reaches={skip_reaches}, "
            f"max_seconds={max_seconds}) ===",
            flush=True,
        )
        backfill_universe(
            skip_if_at_least=skip_val,
            skip_if_reaches=skip_reaches,
            max_seconds=max_seconds,
        )
        return 0
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"=== IV backfill for {ticker} ===")
    ivs = backfill_one_ticker(ticker)
    print(f"\nWrote {len(ivs)} IV entries for {ticker}")
    if ivs:
        dates = sorted(ivs.keys())
        print(f"  Range: {dates[0]} → {dates[-1]}")
        print("  First 3:", [(str(d), f"{ivs[d]:.4f}") for d in dates[:3]])
        print("  Last 3:",  [(str(d), f"{ivs[d]:.4f}") for d in dates[-3:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
