"""Duplicate Master/runtime instances are rejected before polling starts."""

from __future__ import annotations

import stat

import pytest

from Ops.process_lock import (
    InsecureLockDirectoryError,
    ProcessAlreadyRunningError,
    ProcessLock,
)


def test_same_instance_cannot_be_acquired_twice(tmp_path) -> None:
    directory = tmp_path / "locks"
    first = ProcessLock(directory, "runtime-4-0").acquire()
    try:
        with pytest.raises(ProcessAlreadyRunningError):
            ProcessLock(directory, "runtime-4-0").acquire()
        other = ProcessLock(directory, "runtime-4-1").acquire()
        other.release()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "runtime-4-0.lock").stat().st_mode) == 0o600
    finally:
        first.release()
    replacement = ProcessLock(directory, "runtime-4-0").acquire()
    replacement.release()


def test_insecure_lock_directory_and_bad_name_are_rejected(tmp_path) -> None:
    directory = tmp_path / "public"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(InsecureLockDirectoryError):
        ProcessLock(directory, "master").acquire()
    with pytest.raises(ValueError):
        ProcessLock(tmp_path / "safe", "../escape")
