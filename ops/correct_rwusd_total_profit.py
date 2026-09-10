"""Audit and remove legacy RWUSD ``totalProfit`` from minute snapshots.

The Binance RWUSD account endpoint exposes both the current ``rwusdAmount``
and an account-lifetime ``totalProfit``.  Older NAV code valued their sum, so a
reused account could inherit RWUSD profit earned before the monitored strategy
started.  This tool mirrors that old conversion with historical Binance minute
prices and subtracts only the ``totalProfit`` component from ``actual_equity``.

The default mode is read-only.  Pass ``--execute`` only after reviewing the
generated audit CSV and stopping the account monitor service.
"""

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import ACCOUNTS_CONFIG_PATH, RUNTIME_LOG_DIR
from core.account_registry import get_account_registry
from core.exchange_accounts.binance import BinanceExchangeAccount


BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
RWUSD_ACTIVITY_PATHS = {
    "rewards": "rwusd/history/rewardsHistory",
    "subscriptions": "rwusd/history/subscriptionHistory",
    "redemptions": "rwusd/history/redemptionHistory",
}
MAX_HISTORY_SPAN_DAYS = 183
ONE_MINUTE_MS = 60_000


@dataclass(frozen=True)
class RwusdAudit:
    rwusd_amount: float
    total_profit: float
    activity_counts: Dict[str, int]


def parse_timestamp(value: str, timezone_name: str) -> pd.Timestamp:
    """Parse a CLI timestamp and return it as a timezone-aware UTC value."""
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(ZoneInfo(timezone_name))
    return parsed.tz_convert("UTC")


def snapshot_minutes_utc(values: Iterable, timezone_name: str) -> pd.DatetimeIndex:
    """Interpret naive snapshot timestamps in the configured CSV timezone."""
    parsed = pd.DatetimeIndex(pd.to_datetime(values, errors="coerce"))
    if parsed.isna().any():
        raise ValueError(f"snapshot contains {int(parsed.isna().sum())} invalid timestamps")
    if parsed.tz is None:
        parsed = parsed.tz_localize(ZoneInfo(timezone_name), ambiguous="raise", nonexistent="raise")
    else:
        parsed = parsed.tz_convert("UTC")
    return parsed.tz_convert("UTC").floor("min")


class BinanceMinutePriceClient:
    """Fetch historical one-minute close prices from Binance public klines."""

    def __init__(self, session: Optional[requests.Session] = None, timeout: float = 15.0):
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch_closes(
        self,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.Series:
        if end < start:
            raise ValueError("price end must not be before start")

        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        prices = {}

        while cursor <= end_ms:
            response = self.session.get(
                BINANCE_KLINES_URL,
                params={
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError(f"unexpected Binance kline response for {symbol}: {rows!r}")
            if not rows:
                break

            for row in rows:
                minute = pd.Timestamp(int(row[0]), unit="ms", tz="UTC")
                prices[minute] = float(row[4])

            next_cursor = int(rows[-1][0]) + ONE_MINUTE_MS
            if next_cursor <= cursor:
                raise RuntimeError(f"Binance kline pagination did not advance for {symbol}")
            cursor = next_cursor

        result = pd.Series(prices, dtype="float64", name=symbol).sort_index()
        if result.empty:
            raise ValueError(f"Binance returned no {symbol} prices for {start} through {end}")
        return result


def build_price_frame(
    minutes: pd.DatetimeIndex,
    ccy: str,
    client: BinanceMinutePriceClient,
) -> pd.DataFrame:
    """Return exact-minute prices needed to mirror the old NAV conversion."""
    unique_minutes = pd.DatetimeIndex(minutes.unique()).sort_values()
    usdc = client.fetch_closes("USDCUSDT", unique_minutes[0], unique_minutes[-1])
    frame = pd.DataFrame(index=unique_minutes)
    frame["USDCUSDT"] = usdc.reindex(unique_minutes)

    if ccy == "BTC":
        btc = client.fetch_closes("BTCUSDT", unique_minutes[0], unique_minutes[-1])
        frame["BTCUSDT"] = btc.reindex(unique_minutes)
    elif ccy == "USDT":
        frame["BTCUSDT"] = float("nan")
    else:
        raise ValueError(f"unsupported account currency for correction: {ccy}")

    required = ["USDCUSDT"] + (["BTCUSDT"] if ccy == "BTC" else [])
    missing = frame[required].isna().any(axis=1)
    if missing.any():
        examples = ", ".join(str(value) for value in frame.index[missing][:5])
        raise ValueError(
            f"missing Binance minute prices for {int(missing.sum())} timestamps; examples: {examples}"
        )
    return frame


def correction_in_account_ccy(
    total_profit: float,
    ccy: str,
    usdc_usdt: pd.Series,
    btc_usdt: pd.Series,
) -> pd.Series:
    """Mirror the old USDC->USDT->[BTC] conversion for totalProfit only."""
    correction_usdt = float(total_profit) * usdc_usdt.astype(float)
    if ccy == "USDT":
        return correction_usdt
    if ccy == "BTC":
        if (btc_usdt <= 0).any():
            raise ValueError("BTCUSDT prices must be positive")
        return correction_usdt / btc_usdt.astype(float)
    raise ValueError(f"unsupported account currency for correction: {ccy}")


def correct_snapshot_dataframe(
    snapshot: pd.DataFrame,
    *,
    total_profit: float,
    ccy: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    csv_timezone: str,
    price_client: BinanceMinutePriceClient,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Return a corrected snapshot copy and a row-level audit dataframe."""
    required = {"timestamp", "actual_equity", "total_unit", "net_value"}
    missing_columns = required.difference(snapshot.columns)
    if missing_columns:
        raise ValueError(f"snapshot missing columns: {sorted(missing_columns)}")

    minutes = snapshot_minutes_utc(snapshot["timestamp"], csv_timezone)
    selected = (minutes >= start.floor("min")) & (minutes <= end.floor("min"))
    if not selected.any():
        raise ValueError("snapshot has no rows in the requested correction window")

    selected_minutes = minutes[selected]
    prices = build_price_frame(selected_minutes, ccy, price_client)
    row_prices = prices.reindex(selected_minutes)
    correction = correction_in_account_ccy(
        total_profit,
        ccy,
        row_prices["USDCUSDT"].reset_index(drop=True),
        row_prices["BTCUSDT"].reset_index(drop=True),
    )

    selected_index = snapshot.index[selected]
    actual_before = pd.to_numeric(snapshot.loc[selected_index, "actual_equity"], errors="coerce")
    total_unit = pd.to_numeric(snapshot.loc[selected_index, "total_unit"], errors="coerce")
    if actual_before.isna().any() or not actual_before.map(math.isfinite).all():
        raise ValueError("selected actual_equity values must be finite numbers")
    if total_unit.isna().any() or not total_unit.map(math.isfinite).all() or (total_unit <= 0).any():
        raise ValueError("selected total_unit values must be finite and positive")

    correction.index = selected_index
    actual_after = actual_before - correction
    if not actual_after.map(math.isfinite).all() or (actual_after <= 0).any():
        raise ValueError("correction would produce a non-positive or non-finite actual_equity")
    net_after = actual_after / total_unit

    corrected = snapshot.copy()
    corrected.loc[selected_index, "actual_equity"] = actual_after
    corrected.loc[selected_index, "net_value"] = net_after

    audit = pd.DataFrame(
        {
            "timestamp": snapshot.loc[selected_index, "timestamp"].astype(str).values,
            "minute_utc": [value.isoformat() for value in selected_minutes],
            "ccy": ccy,
            "total_profit_usdc": float(total_profit),
            "USDCUSDT": row_prices["USDCUSDT"].to_numpy(),
            "BTCUSDT": row_prices["BTCUSDT"].to_numpy(),
            "actual_equity_before": actual_before.to_numpy(),
            "correction": correction.to_numpy(),
            "actual_equity_after": actual_after.to_numpy(),
            "total_unit": total_unit.to_numpy(),
            "net_value_before": pd.to_numeric(
                snapshot.loc[selected_index, "net_value"], errors="coerce"
            ).to_numpy(),
            "net_value_after": net_after.to_numpy(),
        }
    )
    return corrected, audit


async def _fetch_activity_count(
    account: BinanceExchangeAccount,
    path: str,
    start_ms: int,
    end_ms: int,
) -> int:
    current = 1
    total_rows = 0
    while True:
        response = await account.request(
            path,
            "sapi",
            "GET",
            {
                "startTime": start_ms,
                "endTime": end_ms,
                "current": current,
                "size": 100,
            },
            None,
            None,
            {"sign": True},
        )
        rows = response.get("rows", [])
        total_rows += len(rows)
        reported_total = int(response.get("total", total_rows))
        if not rows or total_rows >= reported_total:
            return reported_total
        current += 1


async def fetch_rwusd_audit(
    account_name: str,
    account_info: dict,
    *,
    start: Optional[pd.Timestamp] = None,
    end: Optional[pd.Timestamp] = None,
) -> RwusdAudit:
    """Fetch current RWUSD fields and optional activity counts for a window."""
    account = BinanceExchangeAccount.from_account_info(account_name, account_info)
    try:
        state = await account.fetch_rwusd_account()
        counts = {}
        if start is not None and end is not None:
            start_ms = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            for label, path in RWUSD_ACTIVITY_PATHS.items():
                counts[label] = await _fetch_activity_count(account, path, start_ms, end_ms)
        return RwusdAudit(
            rwusd_amount=float(state.get("rwusdAmount", 0)),
            total_profit=float(state.get("totalProfit", 0)),
            activity_counts=counts,
        )
    finally:
        await account.close()


def atomic_replace_with_backup(path: Path, dataframe: pd.DataFrame) -> Tuple[Path, Path]:
    """Back up a snapshot and atomically replace it with corrected contents."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.before_rwusd_correction_{stamp}.bak")
    marker = path.with_name(f"{path.name}.rwusd_correction.json")
    if marker.exists():
        raise FileExistsError(f"correction marker already exists: {marker}")
    if backup.exists():
        raise FileExistsError(f"backup already exists: {backup}")

    temp_path = None
    try:
        shutil.copy2(path, backup)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as target:
            temp_path = Path(target.name)
            dataframe.to_csv(target, index=False)
        os.replace(temp_path, path)
        return backup, marker
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if backup.exists() and path.exists():
            backup.unlink(missing_ok=True)
        raise


def write_marker(marker: Path, payload: dict) -> None:
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def live_monitor_pids(runtime_log_dir: Path) -> Sequence[int]:
    """Return live PIDs referenced by account-monitor runtime pid files."""
    live = []
    for pid_file in runtime_log_dir.glob("*.pid"):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
        live.append(pid)
    return live


def select_accounts(accounts: dict, requested: Sequence[str], select_all: bool) -> Dict[str, dict]:
    binance = {
        name: info for name, info in accounts.items() if info.get("exchange_id") == "binance"
    }
    if select_all:
        return dict(sorted(binance.items()))
    unknown = sorted(set(requested).difference(binance))
    if unknown:
        raise ValueError(f"unknown Binance accounts: {', '.join(unknown)}")
    return {name: binance[name] for name in requested}


def default_report_path(account_name: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return RUNTIME_LOG_DIR / f"rwusd_total_profit_{account_name}_{stamp}.csv"


def validate_window(start: pd.Timestamp, end: pd.Timestamp) -> None:
    if end < start:
        raise ValueError("end must not be before start")
    if (end - start).total_seconds() > MAX_HISTORY_SPAN_DAYS * 86400:
        raise ValueError(
            f"RWUSD history API supports at most about {MAX_HISTORY_SPAN_DAYS} days per run"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--accounts", nargs="+", help="Binance account names to audit/correct")
    selection.add_argument("--all", action="store_true", help="audit every Binance account")
    parser.add_argument(
        "--start",
        help="correction start in the CSV timezone; omit for current-state audit only",
    )
    parser.add_argument("--end", help="correction end; defaults to now")
    parser.add_argument(
        "--csv-timezone",
        default="UTC",
        help="timezone of naive snapshot timestamps (default: UTC)",
    )
    parser.add_argument("--report", type=Path, help="audit CSV path (one account only)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="back up and replace source snapshot; default is dry-run",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    ZoneInfo(args.csv_timezone)  # Validate before any network requests.
    if args.all and args.start:
        raise ValueError("--all is audit-only; select explicit --accounts for historical correction")
    if args.execute and not args.start:
        raise ValueError("--execute requires --start")
    if args.report and (args.all or len(args.accounts or []) != 1):
        raise ValueError("--report can only be used with one explicit account")

    registry = get_account_registry(str(ACCOUNTS_CONFIG_PATH))
    selected = select_accounts(registry.local_accounts(), args.accounts or [], args.all)
    if not selected:
        raise ValueError("no Binance accounts selected")

    start = parse_timestamp(args.start, args.csv_timezone) if args.start else None
    end = (
        parse_timestamp(args.end, args.csv_timezone)
        if args.end
        else pd.Timestamp.now(tz="UTC")
    )
    if start is not None:
        validate_window(start, end)

    if args.execute:
        live_pids = live_monitor_pids(Path(RUNTIME_LOG_DIR))
        if live_pids:
            raise RuntimeError(
                "account monitor is still running (PID "
                + ", ".join(map(str, live_pids))
                + "); stop it before --execute"
            )

    for index, (account_name, account_info) in enumerate(selected.items()):
        audit = await fetch_rwusd_audit(
            account_name,
            account_info,
            start=start,
            end=end if start is not None else None,
        )
        activity = sum(audit.activity_counts.values())
        print(
            f"{account_name}: ccy={account_info['ccy']} "
            f"rwusdAmount={audit.rwusd_amount:.8f} totalProfit={audit.total_profit:.8f}"
        )

        if start is None:
            if index + 1 < len(selected) and args.all:
                # The account endpoint has a high request weight.  Throttle the
                # all-account audit to avoid exhausting Binance's IP budget.
                time.sleep(2)
            continue

        if activity:
            details = ", ".join(
                f"{name}={count}" for name, count in audit.activity_counts.items() if count
            )
            raise RuntimeError(
                f"{account_name}: RWUSD activity exists in correction window ({details}); "
                "current totalProfit cannot safely represent every historical minute"
            )
        if audit.total_profit == 0:
            print(f"{account_name}: no totalProfit to correct")
            continue

        source = Path(account_info["minute_snapshot_file"])
        if not source.exists():
            raise FileNotFoundError(f"snapshot not found: {source}")
        snapshot = pd.read_csv(source, encoding="utf-8-sig")
        corrected, row_audit = correct_snapshot_dataframe(
            snapshot,
            total_profit=audit.total_profit,
            ccy=account_info["ccy"],
            start=start,
            end=end,
            csv_timezone=args.csv_timezone,
            price_client=BinanceMinutePriceClient(),
        )

        report = args.report or default_report_path(account_name)
        report.parent.mkdir(parents=True, exist_ok=True)
        if report.exists():
            raise FileExistsError(f"report already exists: {report}")
        row_audit.to_csv(report, index=False)
        print(
            f"{account_name}: rows={len(row_audit)} "
            f"first_correction={row_audit.iloc[0]['correction']:.8f} "
            f"last_correction={row_audit.iloc[-1]['correction']:.8f} report={report}"
        )

        if args.execute:
            backup, marker = atomic_replace_with_backup(source, corrected)
            write_marker(
                marker,
                {
                    "account": account_name,
                    "source": str(source),
                    "backup": str(backup),
                    "report": str(report),
                    "start_utc": start.isoformat(),
                    "end_utc": end.isoformat(),
                    "total_profit_usdc": audit.total_profit,
                    "rows": len(row_audit),
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                },
            )
            print(f"{account_name}: applied; backup={backup}; marker={marker}")
        else:
            print(f"{account_name}: dry-run only; source CSV unchanged")

    return 0


def main() -> None:
    args = build_parser().parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except (ValueError, RuntimeError, FileNotFoundError, FileExistsError, requests.RequestException) as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()
