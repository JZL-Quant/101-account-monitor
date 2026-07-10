# Account Monitor

This directory contains the unified account monitoring service.

## Entry Points

- `account_monitor_app.py`
  - Fetches account equity every minute.
  - Writes minute snapshots to `minute_snapshots/<exchange>_<account>_<ccy>_minute_snapshot.csv`.
  - Starts the FastAPI subscription/dividend service.
  - Exposes Prometheus metrics at `/metrics`.
  - Exposes the subscription/dividend page at `/operations`.
  - Runs the monitor minute/daily scheduler in the same process.
  - Default port: `7000`.
  - Default host: `0.0.0.0`.
  - Override with `MONITOR_NAV_HOST`.
  - Override with `MONITOR_NAV_PORT`.

- `account_monitor.py`
  - Reads the minute CSV files.
  - Updates Prometheus metrics.
  - Calculates annualized returns and sends Feishu cards.
  - Does not start a separate HTTP server; it is loaded by `account_monitor_app.py`.

## Start Scripts

Create the local account configuration from `accounts_config.example.yaml`. Keep
the real `accounts_config.yaml` on the server only; it is ignored by Git because
it contains exchange credentials. Notification and dashboard credentials are
read from `FEISHU_WEBHOOK_URL`, `FEISHU_WEBHOOK_URL_1`, `FEISHU_APP_ID`,
`FEISHU_APP_SECRET`, and `GRAFANA_API_TOKEN`.

```bash
./start_account_monitor.sh
```

Runtime names are exchange-neutral: logs and PID files use `MONITOR_SERVICE_NAME`, defaulting to `account_monitor`.
Before starting the service, the script runs `bootstrap_minute_snapshots.py` only when the configured snapshot directory is missing or contains no `*_minute_snapshot.csv` files. It copies useful old `*_minute_log.csv` data from `../Binance_monitor`, `../Binance_monitor_B`, or `../Gate_monitor` into the new ccy-aware snapshot files.

## Account Notes

- `accounts_config.yaml` contains accounts from both original directories.
- Each account has an `exchange` field. Existing accounts include `exchange: Binance` and `exchange: Gate`.
- Account currency is driven by each account's `ccy`.
- The NAV fetcher returns BTC-denominated equity only when `ccy: BTC`; otherwise it keeps USDT-denominated equity.
- Report groups are generated dynamically from `exchange`, account-name prefix, and `ccy`, for example `Binance_Loro_USDT`.
- Gate accounts share the top-level `Blacklist` list in `accounts_config.yaml`; missing prices for blacklisted Gate currencies are skipped without alerting.

Minute snapshots are read from and written to `minute_snapshots/`.

## Core Structure

- `core/account_registry.py`
  - Owns account config loading and exchange account runtime construction.
  - Keeps `exchange`, `exchange_id`, `exchange_label`, `account_group`, and `ccy` on each account.
  - Caches config loading per config path so repeated callers share one parsed account set.
- `core/exchange_accounts/`
  - Owns exchange account runtime classes.
  - `BaseExchangeAccount` owns shared CSV snapshots, subscription handling, dividend handling, and API request extension points.
  - `BinanceExchangeAccount` contains the current Binance ccxt calls and actual-equity calculation.
  - `GateExchangeAccount` contains the Gate.io SDK calls and actual-equity calculation.
- `core/runtime_logging.py`
  - Owns shared rotating runtime logger setup and print-to-logger adapter.
  - Supports `DEBUG`, `MESSAGE`, `WARNING`, and `ERROR`.
  - `RUNTIME_LOG_LEVEL` controls the minimum emitted level; the default is `ERROR`.
  - A per-logger override is also supported, for example `ACCOUNT_MONITOR_LOG_LEVEL=ERROR`.
- `core/metrics.py`
  - Owns per-account Prometheus metric creation and reads/writes.
- `core/scheduler.py`
  - Owns startup, minute, and daily scheduling.
  - Timing is controlled by `BINANCE_DAILY_HOUR`, `BINANCE_DAILY_MINUTE`, and `BINANCE_RUN_DAILY_ON_STARTUP`.
- `core/feishu.py`
  - Owns Feishu webhook delivery, retry handling, tenant token retrieval, image upload, and the notifier instance.
- `core/report_cards.py`
  - Owns report card schema construction.

## NAV Service Structure

- `account_monitor_app.py`
  - Thin entrypoint only: initializes account state, creates the FastAPI app, starts background tasks, and runs uvicorn.
- `nav_service/app_factory.py`
  - Creates the FastAPI app, mounts static files, and wires route registration.
- `nav_service/routes.py`
  - Owns `/operations`, `/accounts/new`, `/accounts`, `/metrics`, subscription, and dividend routes.
- `nav_service/state.py`
  - Owns account registry state, account option rendering, new-account config appends, validation, reloads, and per-account update tasks.
- `nav_service/schemas.py`
  - Owns request models for JSON subscription and dividend APIs.

The return calculations remain in `account_monitor.py`; notification delivery and card rendering are kept outside the entrypoint.

## Adding An Exchange

1. Add `core/exchange_accounts/<exchange>.py` with a subclass of `BaseExchangeAccount`.
   - Implement `get_actual_equity()`.
   - Reuse `BaseExchangeAccount.record_minute_snapshot()` and `get_last_actual_equity_from_csv()` for snapshot writing and fallback behavior.
   - Override `from_account_info()` if the exchange needs extra config fields such as `blacklist`.
2. Export the class from `core/exchange_accounts/__init__.py`.
3. Register it in `core/account_registry.py` by adding it to `EXCHANGE_ACCOUNT_BY_ID`.
4. Add accounts to `accounts_config.yaml` with these required fields:

```yaml
Account_Name:
  key: your_api_key
  secret: your_secret_key
  initial_unit: 1000000
  account_type: account
  exchange: Gate
  interest_rate: 0
  client: ClientName
  ccy: USDT
```

5. If the exchange has old minute logs, add its source directory to `LEGACY_DIRS` in `bootstrap_minute_snapshots.py` and make sure old file names match `<Exchange>_<account>_minute_log.csv` or `<Exchange>_<account>_minute_snapshot.csv`.
6. If accounts can be added from `/accounts/new`, update `nav_service/state.py` only when the exchange needs a different default `account_type` or display source.
7. Validate the wiring:

```bash
python -m py_compile core/account_registry.py core/exchange_accounts/base.py core/exchange_accounts/<exchange>.py nav_service/state.py
python bootstrap_minute_snapshots.py
```
