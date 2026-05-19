# Data_Collector

Temporary helper for the (private) **Options-Trading-Bot-v2** project.

It collects **implied-volatility history** on free public-repo GitHub
Actions and commits it to `iv/<ticker>.parquet`. IV is public market
data — nothing private lives here: no account values, strategy,
candidates, or trade data.

## Two workflows

**Collect IV data** (`collect.yml`) — the **backfill**. Reconstructs a
~400-day IV history per ticker via Black-Scholes back-solve. ~3 hours.
Run it once to build history; rerun occasionally to rebuild.

- Actions → **Collect IV data** → **Run workflow**.
- Leave `skip_if_reaches` blank for a full backfill; set a `YYYY-MM-DD`
  date to skip tickers whose cache already reaches that far back.

**Daily IV update** (`update.yml`) — keeps each cache's **endpoint
fresh**. Reads today's ATM-put IV straight off the live snapshot
(no back-solve) and appends one point per ticker. ~15 minutes.

- Runs **automatically** each weekday (~5:30 PM ET).
- Can also be triggered manually: Actions → **Daily IV update** →
  **Run workflow**.

Why both: the backfill builds the *history* but lags 1-3 days at the
tip (recent option bars aren't available yet). IVR ranks against the
*latest* point, so that point must be fresh — the daily update
provides it.

## Layout

| File | Role |
|------|------|
| `iv_backfill.py` | the backfill (history reconstruction) |
| `iv_update.py` | the daily update (fresh endpoint) |
| `market_data.py` | Alpaca clients, daily-bar fetch, chain fetch, universe loaders |
| `alpaca_resilience.py` | request timeout + retry wrappers for Alpaca calls |
| `black_scholes.py` | put pricing + IV solver |
| `clock.py` | America/New_York time helpers |
| `universe/` | the ticker lists |
| `iv/` | output — committed by the workflows |

## Disposable

Once the main project is complete, IV collection folds back into the
private repo. Then: disable the schedules (or just delete this repo).
