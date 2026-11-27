"""Create a SQLite snapshot, then compare it with a stopped (frozen) source.

Usage:
    python sqlite_snapshot.py backup SOURCE DESTINATION [--timeout 30]
    python sqlite_snapshot.py verify SOURCE SNAPSHOT [--timeout 30]

Backup is safe while SOURCE is live. Verify is meaningful only after all writers
have stopped; it never compares a live source to an earlier point-in-time copy.
No row values are printed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile


def _readonly(path: Path, timeout: float) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"SQLite file does not exist: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
    connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
    return connection


def _integrity(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise ValueError("SQLite integrity_check failed")


def _worker(source: Path, temporary: Path, timeout: float) -> None:
    with closing(_readonly(source, timeout)) as src, closing(sqlite3.connect(temporary, timeout=timeout)) as dst:
        src.backup(dst, pages=256, sleep=0.05)
        _integrity(dst)


def backup(source: Path, destination: Path, timeout: float = 30.0) -> None:
    source, destination = source.resolve(), destination.absolute()
    if timeout <= 0 or not source.is_file():
        raise ValueError("Source must be an existing file and timeout must be positive")
    if source == destination or destination.exists():
        raise FileExistsError(f"Destination already exists or is the source: {destination}")
    if not destination.parent.is_dir():
        raise ValueError(f"Destination directory does not exist: {destination.parent}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "_worker", str(source), str(temporary), str(timeout)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"SQLite backup failed (exit {result.returncode}): {result.stderr.strip()}")
        # Hard-link publication is atomic and fails if another process created DESTINATION.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _value(value: object) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, int):
        payload = str(value).encode("ascii")
        tag = b"I"
    elif isinstance(value, float):
        payload = struct.pack(">d", value)
        tag = b"F"
    elif isinstance(value, str):
        payload = value.encode("utf-8")
        tag = b"T"
    elif isinstance(value, bytes):
        payload = value
        tag = b"B"
    else:
        raise TypeError(f"Unsupported SQLite value type: {type(value).__name__}")
    return tag + len(payload).to_bytes(8, "big") + payload


def _schema(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str | None], ...]:
    return tuple(connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name, tbl_name, sql"
    ))


def _rows(connection: sqlite3.Connection, table: str) -> Counter[bytes]:
    escaped = table.replace('"', '""')
    result: Counter[bytes] = Counter()
    for row in connection.execute(f'SELECT * FROM "{escaped}"'):
        digest = hashlib.sha256()
        digest.update(len(row).to_bytes(8, "big"))
        for value in row:
            digest.update(_value(value))
        result[digest.digest()] += 1
    return result


def verify(source: Path, snapshot: Path, timeout: float = 30.0) -> None:
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    with closing(_readonly(source, timeout)) as src, closing(_readonly(snapshot, timeout)) as dst:
        src.execute("BEGIN")
        dst.execute("BEGIN")
        _integrity(src)
        _integrity(dst)
        source_schema, snapshot_schema = _schema(src), _schema(dst)
        if source_schema != snapshot_schema:
            raise ValueError("SQLite schema mismatch")
        for kind, table, _, _ in source_schema:
            if kind == "table":
                source_rows, snapshot_rows = _rows(src, table), _rows(dst, table)
                if sum(source_rows.values()) != sum(snapshot_rows.values()) or source_rows != snapshot_rows:
                    raise ValueError(f"SQLite table mismatch: {table}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args and args[0] == "_worker":
            _worker(Path(args[1]), Path(args[2]), float(args[3]))
            return 0
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("command", choices=("backup", "verify"))
        parser.add_argument("source", type=Path)
        parser.add_argument("destination", type=Path, help="New snapshot, or existing snapshot for verify")
        parser.add_argument("--timeout", type=float, default=30.0, help="Busy limit; also total backup limit, seconds")
        parsed = parser.parse_args(args)
        if parsed.command == "backup":
            backup(parsed.source, parsed.destination, parsed.timeout)
        else:
            verify(parsed.source, parsed.destination, parsed.timeout)
        print(f"SQLite {parsed.command} OK")
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError, subprocess.TimeoutExpired, IndexError) as exc:
        print(f"SQLite operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
