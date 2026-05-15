"""Daily IV update — append today's ATM-put IV for every ticker.

Reads the live option-chain snapshot (direct IV, no Black-Scholes
back-solve) and appends one fresh point per ticker to
`iv/<ticker>.parquet`.

This is what keeps each cache's *endpoint* current. The backfill
(`iv_backfill.py`) reconstructs the year of *history* but lags 1-3+
days at the tip — recent option bars aren't available yet. IVR ranks
against the latest point, so that latest point has to be fresh; this
job provides it.

Run: `python -m iv_update`
"""
from __future__ import annotations

from market_data import (
    get_options_client,
    load_curated_universe,
    load_manual_universe,
    update_iv_today,
)


def main() -> int:
    oc = get_options_client()
    tickers = sorted(
        set(load_curated_universe()) | set(load_manual_universe())
    )
    print(f"Updating IV for {len(tickers)} tickers...", flush=True)

    ok = miss = err = 0
    for i, ticker in enumerate(tickers, 1):
        try:
            iv, meta = update_iv_today(ticker, oc)
            if iv is None:
                print(f"  [{i}/{len(tickers)}] {ticker}: no ATM put "
                      f"(chain_size={meta['chain_size']})", flush=True)
                miss += 1
            else:
                print(f"  [{i}/{len(tickers)}] {ticker}: IV={iv:.4f}",
                      flush=True)
                ok += 1
        except Exception as exc:
            print(f"  [{i}/{len(tickers)}] {ticker}: ERROR {exc}", flush=True)
            err += 1

    print(f"\nSummary: {ok} updated · {miss} no-data · {err} errored",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
