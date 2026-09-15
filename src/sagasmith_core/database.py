"""Database runtime for the general TTRPG domain."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from sagasmith_core.models import Base
from sagasmith_core.paths import data_root


def sqlite_database_url(path: str | Path) -> str:
    return f"sqlite+pysqlite:///{Path(path).expanduser().resolve().as_posix()}"


def default_database_url() -> str:
    if configured := os.environ.get("SAGASMITH_DATABASE_URL"):
        return configured
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    return sqlite_database_url(root / "ttrpgbase.db")


def alembic_config(database_url: str) -> Config:
    from importlib.resources import files

    config = Config()
    migrations = files("sagasmith_core").joinpath("migrations")
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


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

    def require_owner(self) -> None:
        if self.closed or self.owner != _execution_owner():
            raise RuntimeError("unit of work is closed or belongs to another execution owner")


class Database:
    """Own the general TTRPG database and transactional session factory."""

    def __init__(self, url: str | None = None, *, echo: bool = False) -> None:
        self.url = url or default_database_url()
        connect_args = {"check_same_thread": False} if self.url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(
            self.url,
            connect_args=connect_args,
            pool_pre_ping=True,
            echo=echo,
        )
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._configure_sqlite_connection)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self._ambient_session: ContextVar[Session | None] = ContextVar(
            f"sagasmith_database_session_{id(self)}",
            default=None,
        )
        self._ambient_immediate: ContextVar[bool] = ContextVar(
            f"sagasmith_database_immediate_{id(self)}",
            default=False,
        )
        self._ambient_uow: ContextVar[UnitOfWork | None] = ContextVar(
            f"sagasmith_uow_{id(self)}", default=None
        )

    def require_independent_work(self) -> None:
        if self._ambient_uow.get() is not None:
            raise RuntimeError("external work cannot run inside an uncommitted unit of work")

    @contextmanager
    def unit_of_work(self, *, immediate: bool = False) -> Iterator[UnitOfWork]:
        with self.transaction(immediate=immediate):
            work = self._ambient_uow.get()
            assert work is not None
            yield work

    @contextmanager
    def savepoint(self, work: UnitOfWork) -> Iterator[Session]:
        work.require_owner()
        if work is not self._ambient_uow.get():
            raise RuntimeError("savepoint requires the active unit of work")
        with work.session.begin_nested():
            previous = work.rollback_only
            try:
                yield work.session
            except BaseException:
                work.rollback_only = previous
                raise

    @staticmethod
    def _configure_sqlite_connection(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def create_schema(self) -> None:
        Base.metadata.create_all(bind=self.engine)

    def upgrade_schema(self, revision: str = "head") -> None:
        command.upgrade(alembic_config(self.url), revision)

    def drop_schema(self) -> None:
        Base.metadata.drop_all(bind=self.engine)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[Session]:
        ambient = self._ambient_session.get()
        if ambient is not None:
            work = self._ambient_uow.get()
            assert work is not None
            work.require_owner()
            if immediate and not self._ambient_immediate.get():
                raise RuntimeError(
                    "an immediate transaction is required before entering this ambient transaction"
                )
            try:
                yield ambient
            except BaseException as error:
                work.rollback_only = True
                work.failure = error
                raise
            return
        session = self.session_factory()
        work = UnitOfWork(session, _execution_owner())
        work_token = self._ambient_uow.set(work)
        token = self._ambient_session.set(session)
        immediate_token = self._ambient_immediate.set(immediate)
        try:
            with session.begin():
                if immediate and self.engine.dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield session
                if work.rollback_only:
                    raise RuntimeError(
                        "unit of work is rollback-only after a nested failure"
                    ) from work.failure
        finally:
            work.closed = True
            self._ambient_uow.reset(work_token)
            self._ambient_immediate.reset(immediate_token)
            self._ambient_session.reset(token)
            session.close()

    def dependency(self) -> Generator[Session, None, None]:
        session = self.session_factory()
        try:
            yield session
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
