"""Versioned migration runner: atomic per-migration apply + checksum guard."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from Database.connection import connect, transaction
from Shared.timeutils import iso_utc, utcnow

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "Migrations"


class MigrationError(RuntimeError):
    """A migration file failed; the database was left unchanged for it."""


class MigrationChecksumError(MigrationError):
    """An already-applied migration file was modified on disk."""


def file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL)"
    )


def applied_checksums(conn: sqlite3.Connection) -> dict[str, str]:
    _ensure_table(conn)
    return {row[0]: row[1] for row in conn.execute("SELECT version, checksum FROM schema_migrations")}


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    return set(applied_checksums(conn))


def pending_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(migrations_dir.glob("*.sql"), key=lambda path: path.name)


class _UnclosedStringError(MigrationError):
    """A single-quoted string was opened but not closed before end of file."""


class _UnclosedBlockCommentError(MigrationError):
    """A block comment (/*) was opened but not closed before end of file."""


def split_statements(sql: str) -> list[str]:
    """Split SQL into statements, handling quotes, escapes, comments, triggers."""
    raw: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    while i < n:
        ch = sql[i]
        # Line comment
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i)
            if buf and not buf[-1].isspace():
                buf.append("\n")
            i = n if end == -1 else end + 1
            continue
        # Block comment
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                raise _UnclosedBlockCommentError("unclosed block comment at end of file")
            if buf and not buf[-1].isspace():
                buf.append(" ")
            i = end + 2
            continue
        # Single-quoted string (handle '' escapes)
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 1
                        buf.append(sql[i])
                    else:
                        i += 1
                        break
                i += 1
            else:
                raise _UnclosedStringError("unclosed single-quoted string at end of file")
            continue
        # Double-quoted identifier (handle "" escapes)
        if ch == '"':
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 1
                        buf.append(sql[i])
                    else:
                        i += 1
                        break
                i += 1
            else:
                raise MigrationError("unclosed double-quoted identifier at end of file")
            continue
        # Statement separator
        if ch == ";":
            candidate = "".join(buf).strip()
            if candidate:
                raw.append(candidate)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        raw.append(tail)

    # Merge compound statements using sqlite3.complete_statement.
    merged: list[str] = []
    segment: list[str] = []
    for token in raw:
        segment.append(token)
        candidate = ";\n".join(segment) + ";"
        if sqlite3.complete_statement(candidate):
            merged.append(";\n".join(segment))
            segment = []
    if segment:
        merged.append(";\n".join(segment))
    return merged


def verify_checksums(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> None:
    recorded = applied_checksums(conn)
    available = {path.stem: path for path in pending_migrations(migrations_dir)}
    for version, expected in recorded.items():
        path = available.get(version)
        if path is None:
            continue
        actual = file_checksum(path)
        if actual != expected:
            raise MigrationChecksumError(
                f"migration {version} changed after being applied "
                f"(recorded {expected[:12]}..., now {actual[:12]}...)"
            )


def apply_migration(conn: sqlite3.Connection, path: Path) -> str:
    version = path.stem
    checksum = file_checksum(path)
    statements = split_statements(path.read_text(encoding="utf-8"))
    if not statements:
        raise MigrationError(f"migration {version} is empty")
    try:
        with transaction(conn):
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, checksum)"
                " VALUES (?, ?, ?)",
                (version, iso_utc(utcnow()), checksum),
            )
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(f"migration {version} failed: {type(exc).__name__}") from exc
    return version


def migrate(db_path: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    applied: list[str] = []
    conn = connect(db_path)
    try:
        verify_checksums(conn, migrations_dir)
        done = set(applied_checksums(conn))
        for path in pending_migrations(migrations_dir):
            if path.stem in done:
                continue
            applied.append(apply_migration(conn, path))
        return applied
    finally:
        conn.close()
