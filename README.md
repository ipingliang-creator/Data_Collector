# Data_Collector

Temporary helper for the (private) **Options-Trading-Bot-v2** project.

It runs the bot's **IV backfill** on free public-repo GitHub Actions and
commits the resulting implied-volatility cache to `iv/`. Implied
volatility is public market data — nothing private lives here: no
account values, strategy, candidates, or trade data.

## Run it

Actions tab → **Collect IV data** → **Run workflow**.

- Leave `skip_if_reaches` **blank** for a full backfill of the whole
  universe (~3 hours).
- Set it to a `YYYY-MM-DD` date to skip tickers whose cache already
  reaches back to that date — use this to resume a partial run.

Output lands in `iv/<ticker>.parquet` (columns: `date`, `iv`) and is
committed back to this repo by the workflow.

## Layout

| File | Role |
|------|------|
| `iv_backfill.py` | the backfill (copied from the bot's `skills/`) |
| `market_data.py` | Alpaca clients, daily-bar fetch, universe loaders |
| `black_scholes.py` | put pricing + IV solver |
| `clock.py` | America/New_York time helpers |
| `universe/` | the ticker lists |

## Disposable

Once the main project is complete, IV collection folds back into the
private repo and this repo can be deleted.
