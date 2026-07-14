import csv
import os
import shutil

from core.account_registry import get_account_registry
from config.settings import ACCOUNTS_CONFIG_PATH, LEGACY_SNAPSHOT_DIRS


LEGACY_DIRS = tuple(map(str, LEGACY_SNAPSHOT_DIRS))


def count_csv_rows(path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        row_count = sum(1 for _ in reader)
    return max(row_count - 1, 0)


def legacy_candidates(account_name, account_info):
    exchange_label = account_info["exchange_label"]
    old_file_name = f"{exchange_label}_{account_name}_minute_log.csv"
    snapshot_file_name = f"{exchange_label}_{account_name}_minute_snapshot.csv"
    for legacy_dir in LEGACY_DIRS:
        yield os.path.join(legacy_dir, old_file_name)
        yield os.path.join(legacy_dir, snapshot_file_name)


def best_legacy_file(account_name, account_info):
    candidates = [
        (count_csv_rows(path), path)
        for path in legacy_candidates(account_name, account_info)
        if os.path.exists(path)
    ]
    if not candidates:
        return 0, None
    return max(candidates, key=lambda item: item[0])


def bootstrap_account_snapshot(account_name, account_info):
    target_path = account_info["minute_snapshot_file"]
    source_rows, source_path = best_legacy_file(account_name, account_info)
    if not source_path or source_rows <= 0:
        return "skip", account_name, "legacy file not found"

    target_rows = count_csv_rows(target_path)
    if os.path.exists(target_path) and target_rows >= source_rows:
        return "skip", account_name, f"target rows {target_rows} >= legacy rows {source_rows}"

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    shutil.copy2(source_path, target_path)
    return "copy", account_name, f"{source_rows} rows from {source_path}"


def main():
    accounts = get_account_registry(str(ACCOUNTS_CONFIG_PATH)).local_accounts()

    copied = 0
    skipped = 0
    for account_name, account_info in accounts.items():
        action, _, detail = bootstrap_account_snapshot(account_name, account_info)
        if action == "copy":
            copied += 1
            print(f"[copy] {account_name}: {detail}")
        else:
            skipped += 1
            print(f"[skip] {account_name}: {detail}")

    print(f"bootstrap minute snapshots done: copied={copied}, skipped={skipped}")


if __name__ == "__main__":
    main()
