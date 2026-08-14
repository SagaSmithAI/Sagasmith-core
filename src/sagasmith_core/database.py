"""Database runtime for the general TTRPG domain."""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
    def transaction(self) -> Iterator[Session]:
        ambient = self._ambient_session.get()
        if ambient is not None:
            yield ambient
            return
        session = self.session_factory()
        token = self._ambient_session.set(session)
        try:
            with session.begin():
                yield session
        finally:
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
