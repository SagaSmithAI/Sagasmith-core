"""Execution ownership without a dependency on a storage adapter."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

Session = Any


def _execution_owner() -> tuple[int, object]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@dataclass
class UnitOfWork:
    session: Session
    owner: tuple[int, object]
    rollback_only: bool = False
    failure: BaseException | None = None
    closed: bool = False
    write_count: int = 0

    def require_owner(self) -> None:
        if self.closed or self.owner != _execution_owner():
            raise RuntimeError("unit of work is closed or belongs to another execution owner")
