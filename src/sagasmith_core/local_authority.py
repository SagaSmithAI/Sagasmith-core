"""Process-lifetime ownership for a local database, without a long SQL transaction."""

from __future__ import annotations

import os
from pathlib import Path


class DatabaseAuthority:
    """Shared deployments coexist; an exclusive local authority excludes both modes.

    The OS releases the lock on process death. Keep the sidecar inode in place;
    deleting lock files can allow two processes to own different inodes.
    """

    def __init__(self, database_path: str, *, exclusive: bool = False) -> None:
        path = Path(database_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(str(path) + ".authority.lock", "a+b")
        try:
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes

                class Overlapped(ctypes.Structure):
                    _fields_ = [
                        ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE),
                    ]

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                lock = kernel.LockFileEx
                lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
                lock.restype = wintypes.BOOL
                self._overlapped = Overlapped()
                handle = msvcrt.get_osfhandle(self._file.fileno())
                if not lock(handle, 1 | (2 if exclusive else 0), 0, 1, 0,
                            ctypes.byref(self._overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_NB | (
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                ))
        except OSError as exc:
            self.close()
            raise RuntimeError(
                "database is owned by an incompatible authority; close the other "
                "local or shared runtime before opening this save"
            ) from exc

    def close(self) -> None:
        self._file.close()
