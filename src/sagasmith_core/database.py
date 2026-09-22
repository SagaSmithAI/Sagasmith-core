"""Database runtime for the general TTRPG domain."""

from __future__ import annotations

import os
import time
import weakref
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from sagasmith_core.local_authority import DatabaseAuthority
from sagasmith_core.models import Base
from sagasmith_core.paths import data_root
from sagasmith_core.work import UnitOfWork, _execution_owner


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


class Database:
    """Own the general TTRPG database and transactional session factory."""

    def __init__(
        self, url: str | None = None, *, echo: bool = False, state_extensions: tuple = (),
        local_authority: bool = False,
    ) -> None:
        identities = [(item.system_id, item.id) for item in state_extensions]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate state extension identity")
        if any(
            not item.id or not item.system_id or item.schema_version < 1
            for item in state_extensions
        ):
            raise ValueError("invalid state extension identity or schema version")
        self.state_extensions = tuple(state_extensions)
        self.url = url or default_database_url()
        connect_args = {"check_same_thread": False} if self.url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(
            self.url,
            connect_args=connect_args,
            pool_pre_ping=True,
            echo=echo,
        )
        self._authority = None
        database_path = self.engine.url.database
        if self.engine.dialect.name == "sqlite" and database_path not in {None, "", ":memory:"}:
            try:
                self._authority = DatabaseAuthority(database_path, exclusive=local_authority)
            except BaseException:
                self.engine.dispose()
                raise
            self._authority_finalizer = weakref.finalize(self, self._authority.close)
        elif local_authority:
            self.engine.dispose()
            raise ValueError("local authority requires a file-backed SQLite database")
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
        event.listen(self.engine, "before_cursor_execute", self._track_command_writes)
        self._query_metrics = ContextVar(f"query_metrics_{id(self)}", default=None)
        event.listen(self.engine, "after_cursor_execute", self._record_query_time)

    @contextmanager
    def measure(self):
        """Count SQL work without retaining query text or keeping transactions open."""
        metrics = {"queries": 0, "elapsed_ms": 0.0}
        token = self._query_metrics.set(metrics)
        try:
            yield metrics
        finally:
            self._query_metrics.reset(token)

    def _record_query_time(self, connection, cursor, statement, parameters, context, executemany):
        metrics = self._query_metrics.get()
        if metrics is not None:
            metrics["queries"] += 1
            metrics["elapsed_ms"] += (time.perf_counter() - context._sagasmith_query_start) * 1000

    def _track_command_writes(
        self, connection, cursor, statement, parameters, context, executemany
    ):
        if self._authority is not None and not self._authority_finalizer.alive:
            raise RuntimeError("database authority was released; create a new Database")
        if self._query_metrics.get() is not None:
            context._sagasmith_query_start = time.perf_counter()
        work = self._ambient_uow.get()
        if work is None:
            return
        sql = statement.lstrip().upper()
        if (
            context.isinsert
            or context.isupdate
            or context.isdelete
            or context.isddl
            or not (
                sql.startswith(("SELECT", "EXPLAIN", "PRAGMA", "BEGIN", "SAVEPOINT", "RELEASE"))
            )
        ):
            work.write_count += 1

    def require_independent_work(self) -> None:
        if self._ambient_uow.get() is not None:
            raise RuntimeError("external work cannot run inside an uncommitted unit of work")

    def work_for_session(self, session: Session) -> UnitOfWork:
        """Validate a legacy consumer session without opening or inheriting a transaction."""
        work = self._ambient_uow.get()
        if work is None or work.session is not session:
            raise RuntimeError("session does not belong to this database's active unit of work")
        work.require_owner()
        return work

    @contextmanager
    def operation(self, work: UnitOfWork) -> Iterator[Session]:
        """Join an explicitly supplied owner; a failed operation poisons its commit."""
        work.require_owner()
        if work is not self._ambient_uow.get():
            raise RuntimeError("operation requires this database's active unit of work")
        if work.rollback_only:
            raise RuntimeError("unit of work is rollback-only") from work.failure
        try:
            yield work.session
        except BaseException as error:
            work.rollback_only = True
            work.failure = error
            raise

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
            previous_failure = work.failure
            try:
                yield work.session
            except BaseException:
                work.rollback_only = previous
                work.failure = previous_failure
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
        if self._authority is not None and not self._authority_finalizer.alive:
            raise RuntimeError("database authority was released; create a new Database")
        command.upgrade(alembic_config(self.url), revision)

    def drop_schema(self) -> None:
        Base.metadata.drop_all(bind=self.engine)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[Session]:
        ambient = self._ambient_session.get()
        inherited_work = self._ambient_uow.get()
        # Python 3.14 free-threaded builds copy ContextVars into new threads.
        # A child thread must open its own SQL transaction, while an async task
        # on the same thread still cannot silently borrow its parent's UoW.
        if inherited_work is not None and inherited_work.owner[0] != _execution_owner()[0]:
            ambient = None
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
        session.info["state_extensions"] = self.state_extensions
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
        if self._authority is not None:
            self._authority_finalizer()
