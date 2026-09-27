"""SQLite connection policy."""

import sqlite3
from pathlib import Path
from typing import Union


DatabasePath = Union[str, Path]


def connect_database(path: DatabasePath) -> sqlite3.Connection:
    """Open a configured SQLite connection for one process."""

    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def connect_readonly(path: DatabasePath) -> sqlite3.Connection:
    """Open an existing SQLite database without permission to modify it."""

    resolved = Path(path).resolve()
    uri = f"{resolved.as_uri()}?mode=ro"
    # FastAPI may enter a synchronous dependency, run its endpoint, and close
    # the dependency on different worker threads. This connection is scoped to
    # one request and is read-only, so allowing that hand-off is safe.
    connection = sqlite3.connect(
        uri, uri=True, timeout=5.0, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA query_only = ON")
    return connection
