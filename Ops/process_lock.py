"""Exclusive process locks for runtime shards."""

from __future__ import annotations

import fcntl
import os
import re
import stat
from pathlib import Path


class ProcessAlreadyRunningError(RuntimeError):
    """Another process owns the same logical service lock."""


class InsecureLockDirectoryError(PermissionError):
    """The dedicated lock directory is accessible by group or others."""


class ProcessLock:
    def __init__(self, directory: Path | str, name: str) -> None:
        self.directory = Path(directory)
        self.name = str(name)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.name):
            raise ValueError("invalid process lock name")
        self._handle = None

    def acquire(self) -> "ProcessLock":
        if self._handle is not None:
            return self
        if self.directory.exists():
            mode = stat.S_IMODE(self.directory.stat().st_mode)
            if mode & 0o077:
                raise InsecureLockDirectoryError("process lock directory must be private")
        else:
            self.directory.mkdir(parents=True, mode=0o700)
        path = self.directory / f"{self.name}.lock"
        handle = path.open("a+", encoding="utf-8")
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ProcessAlreadyRunningError("service instance is already running") from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
