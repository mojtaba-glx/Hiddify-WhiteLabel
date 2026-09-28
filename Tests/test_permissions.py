"""File/dir permission hardening: 0700 data dir, 0600 DB + sidecars."""

from __future__ import annotations

import os
import stat
import uuid

import pytest

from Database.connection import (
    DATA_DIR_MODE,
    DB_FILE_MODE,
    InsecureDatabaseDirectoryError,
    connect,
    enforce_file_modes,
    file_mode,
)


def test_data_dir_is_0700(tmp_path) -> None:
    nested = tmp_path / "data" / "inner"
    db = nested / "wl.db"
    conn = connect(str(db))
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
    finally:
        conn.close()
    assert stat.S_IMODE(os.stat(nested).st_mode) == DATA_DIR_MODE


def test_db_file_is_0600(tmp_path) -> None:
    db = tmp_path / "secure" / "wl.db"
    conn = connect(str(db))
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
    finally:
        conn.close()
    assert file_mode(str(db)) == DB_FILE_MODE


def test_modes_reenforced_on_reconnect(tmp_path) -> None:
    db = tmp_path / "reopen" / "wl.db"
    conn = connect(str(db))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.close()
    os.chmod(str(db), 0o644)
    assert file_mode(str(db)) == 0o644
    conn = connect(str(db))  # reconnect must re-enforce 0600
    try:
        assert file_mode(str(db)) == DB_FILE_MODE
    finally:
        conn.close()


def test_sidecars_are_0600_when_present(tmp_path) -> None:
    db = tmp_path / "sidecar" / "wl.db"
    conn = connect(str(db))
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        enforce_file_modes(str(db))
        for suffix in ("-wal", "-shm", "-journal"):
            candidate = f"{db}{suffix}"
            if os.path.isfile(candidate):
                assert file_mode(candidate) == DB_FILE_MODE
    finally:
        conn.close()


def test_shared_tmp_dir_never_chmodded() -> None:
    # A DB under /tmp (world-writable) is refused; /tmp mode is untouched.
    before = stat.S_IMODE(os.stat("/tmp").st_mode)
    db = f"/tmp/wl_mode_{uuid.uuid4().hex}.db"
    with pytest.raises(InsecureDatabaseDirectoryError):
        connect(db)
    assert stat.S_IMODE(os.stat("/tmp").st_mode) == before
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(f"{db}{suffix}")
        except OSError:
            pass


def test_existing_public_dir_not_chmodded(tmp_path) -> None:
    # A pre-existing directory with 0755 must NOT be flipped to 0700.
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    os.chmod(public_dir, 0o755)
    db = public_dir / "wl.db"
    with pytest.raises(InsecureDatabaseDirectoryError):
        connect(str(db))
    assert stat.S_IMODE(os.stat(public_dir).st_mode) == 0o755
