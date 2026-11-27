import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import sqlite_snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "source.sqlite3"
        self.snapshot = root / "snapshot.sqlite3"

    def test_wal_blob_null_mixed_and_verification(self):
        writer = sqlite3.connect(self.source)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute('CREATE TABLE "odd table" (id INTEGER PRIMARY KEY, value BLOB)')
        writer.execute('CREATE TABLE mixed (value)')
        writer.execute('CREATE TABLE sqliteX_user (value)')
        writer.executemany('INSERT INTO "odd table" VALUES (?, ?)', [(1, b"\x00\xff"), (2, None)])
        writer.executemany("INSERT INTO mixed VALUES (?)", [(None,), (1,), (1.5,), ("1",), (b"1",)])
        writer.commit()
        sqlite_snapshot.backup(self.source, self.snapshot)
        sqlite_snapshot.verify(self.source, self.snapshot)
        with self.assertRaises(FileExistsError):
            sqlite_snapshot.backup(self.source, self.snapshot)
        sqlite_snapshot.verify(self.source, self.snapshot)

    def test_existing_destination_is_untouched(self):
        sqlite3.connect(self.source).close()
        self.snapshot.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            sqlite_snapshot.backup(self.source, self.snapshot)
        self.assertEqual(self.snapshot.read_bytes(), b"existing")

    def test_row_and_schema_mismatch(self):
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("CREATE TABLE items (value)")
            connection.execute("INSERT INTO items VALUES (1)")
            connection.commit()
        sqlite_snapshot.backup(self.source, self.snapshot)
        with closing(sqlite3.connect(self.snapshot)) as connection:
            connection.execute("UPDATE items SET value='1'")
            connection.commit()
        with self.assertRaisesRegex(ValueError, "table mismatch"):
            sqlite_snapshot.verify(self.source, self.snapshot)
        with closing(sqlite3.connect(self.snapshot)) as connection:
            connection.execute("CREATE TABLE extra (value)")
            connection.commit()
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            sqlite_snapshot.verify(self.source, self.snapshot)


if __name__ == "__main__":
    unittest.main()
