"""Read-only integrity checks and consistent online SQLite backups."""

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from .config import load_config


def check_database(path: Path) -> dict:
    connection = _readonly(path)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        counts = {}
        for table in (
            "probe_rounds",
            "probe_samples",
            "latency_hourly",
            "status_intervals",
            "incidents",
            "monitoring_gaps",
        ):
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        return {
            "database": str(path),
            "quick_check": result,
            "size_bytes": path.stat().st_size,
            "counts": counts,
        }
    finally:
        connection.close()


def backup_database(source: Path, destination: Path) -> dict:
    if destination.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = _readonly(source)
    destination_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
        result = destination_connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"backup integrity check failed: {result}")
    except Exception:
        destination_connection.close()
        source_connection.close()
        destination.unlink(missing_ok=True)
        raise
    destination_connection.close()
    source_connection.close()
    os.chmod(destination, 0o640)
    return check_database(destination)


def main(arguments: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Network monitor operations")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/raspi-network-monitor/config.toml"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check", help="run SQLite quick_check")
    check_parser.add_argument("path", type=Path, nargs="?")
    backup_parser = subparsers.add_parser("backup", help="create a consistent backup")
    backup_parser.add_argument("destination", type=Path)
    parsed = parser.parse_args(arguments)

    config = load_config(parsed.config)
    if parsed.command == "check":
        payload = check_database(parsed.path or config.storage.database_path)
    else:
        payload = backup_database(
            config.storage.database_path,
            parsed.destination,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _readonly(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    return sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
