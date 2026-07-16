"""One-time migration of legacy minute snapshot CSV files to eight columns."""

import argparse
import csv
import os
import tempfile
from pathlib import Path

from config.settings import MINUTE_SNAPSHOT_DIR


SNAPSHOT_COLUMNS = [
    "timestamp",
    "actual_equity",
    "total_unit",
    "net_value",
    "dividend_amount",
    "interest_deduction",
    "withdraw_amount",
    "subscription_amount",
]

LEGACY_COLUMNS = [
    "timestamp",
    "actual_equity",
    "total_unit",
    "net_value",
    "dividend_amount",
    "subscription_amount",
]


def migrate_file(path: Path) -> tuple[int, int]:
    """Rewrite one CSV atomically and return (legacy_rows, current_rows)."""
    legacy_rows = 0
    current_rows = 0
    temp_path = None

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source)
            header = next(reader, None)
            if header not in (LEGACY_COLUMNS, SNAPSHOT_COLUMNS):
                raise ValueError(f"{path}: unsupported header: {header}")

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
                writer = csv.writer(target)
                writer.writerow(SNAPSHOT_COLUMNS)

                for line_number, row in enumerate(reader, start=2):
                    if len(row) == len(LEGACY_COLUMNS):
                        writer.writerow([*row[:5], "", "", row[5]])
                        legacy_rows += 1
                    elif len(row) == len(SNAPSHOT_COLUMNS):
                        writer.writerow(row)
                        current_rows += 1
                    else:
                        raise ValueError(
                            f"{path}:{line_number}: expected 6 or 8 fields, saw {len(row)}"
                        )

        os.replace(temp_path, path)
        return legacy_rows, current_rows
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="CSV files to migrate; defaults to every CSV in minute_snapshots",
    )
    args = parser.parse_args()

    paths = args.paths or sorted(Path(MINUTE_SNAPSHOT_DIR).glob("*.csv"))
    if not paths:
        raise SystemExit("No snapshot CSV files found")

    for path in paths:
        legacy_rows, current_rows = migrate_file(path)
        print(f"{path}: migrated={legacy_rows}, already_8_columns={current_rows}")


if __name__ == "__main__":
    main()
