"""SQLite connection with safe defaults.

Transaction policy (explicit, documented):
- Connections run in **autocommit** mode (``isolation_level=None``), so
  there are never implicit/half-open transactions. Every multi-step
  operation must use :func:`transaction` (or a service-level Unit of
  Work built on it). Single-statement repository writes commit at once.
- :func:`transaction` takes ``BEGIN IMMEDIATE`` (write lock up front,
  so concurrent writers serialize instead of deadlocking) and supports
  nesting via ``SAVEPOINT``.

File-permission policy:
- The DB file and its sidecars (``-wal``/``-shm``/``-journal``) are kept
  at mode ``0600``.
- The directory owning a NEW database is created at ``0700``. If the parent
  directory *already exists*, its mode is **never** changed automatically — a
  non-private existing directory is rejected with
  :class:`InsecureDatabaseDirectoryError` instead of being silently chmod'd.
  This prevents a mis-typed ``DATABASE_PATH`` (e.g. ``/home/example.db``
  or any shared dir) from locking a real directory to ``0700``.
- Well-known system dirs (``/``, ``/tmp``, ``/var/tmp``, home) are
  never created or chmod'd.
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_FILE_MODE = 0o600
DATA_DIR_MODE = 0o700

# Paths we must never create or chmod under any circumstance.
_FORBIDDEN_PARENTS = frozenset({"/", "/tmp", "/var/tmp"})
_HOME = Path.home().resolve()


class InsecureDatabaseDirectoryError(PermissionError):
    """The database's parent directory exists but is not private (not 0700)."""


_savepoint_counter = itertools.count(1)


def _is_forbidden(parent: Path) -> bool:
    if str(parent) in _FORBIDDEN_PARENTS or str(parent) == "":
        return True
    try:
        if parent.resolve() == _HOME:
            return True
    except OSError:
        return False
    return False


def _secure_parent_dir(path: Path) -> Path:
    """Return the parent dir, creating it at 0700 only when it does not exist.

    If the parent exists and is not 0700, raise
    :class:`InsecureDatabaseDirectoryError` (no chmod of an existing dir).
    """
    parent = path.parent
    if str(parent) in ("", "."):
        return parent
    parts = parent
    # Build from the first existing ancestor we are allowed to own downward.
    to_create: list[str] = []
    probe = parent
    while not probe.exists() and not _is_forbidden(probe):
        to_create.append(probe.name)
        probe = probe.parent
    if _is_forbidden(probe):
        raise InsecureDatabaseDirectoryError(
            f"refusing to create database under forbidden directory: {parent}"
        )
    if to_create:
        for name in reversed(to_create):
            probe = probe / name
            probe.mkdir(mode=DATA_DIR_MODE)
            os.chmod(probe, DATA_DIR_MODE)
        return parent
    # Parent already exists -> never change its mode; only verify.
    if probe is not None and not stat.S_IMODE(os.stat(probe).st_mode) & 0o077:
        return parent
    # Existing dir with group/other bits set is risky for secret DB files.
    raise InsecureDatabaseDirectoryError(
        f"database parent directory is not private (0700 expected): {parent}"
    )


def enforce_file_modes(db_path: str) -> None:
    """Force 0600 on the DB file and any existing sidecars (wal/shm/journal)."""
    candidates = [db_path, f"{db_path}-wal", f"{db_path}-shm", f"{db_path}-journal"]
    for candidate in candidates:
        try:
            if os.path.isfile(candidate):
                os.chmod(candidate, DB_FILE_MODE)
        except OSError:
            continue


def file_mode(path: str) -> int:
    """Permission bits (e.g. 0o600) of an existing file."""
    return stat.S_IMODE(os.stat(path).st_mode)


def connect(db_path: str) -> sqlite3.Connection:
    """Open an autocommit SQLite connection with FK, WAL, timeout, 0600/0700 modes."""
    path = Path(db_path)
    _secure_parent_dir(path)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA busy_timeout = 5000")
    enforce_file_modes(str(path))
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit Unit-of-Work block: BEGIN IMMEDIATE, commit or full rollback.

    Nested use is safe via SAVEPOINT (inner failure rolls back only to the
    savepoint, outer failure still rolls back everything).
    """
    if conn.in_transaction:
        name = f"wl_sp_{next(_savepoint_counter)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except Exception:
            try:
                conn.execute(f"ROLLBACK TO {name}")
            finally:
                conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    else:
        conn.commit()
