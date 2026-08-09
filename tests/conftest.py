from pathlib import Path

import pytest

from sagasmith_core.database import Database, sqlite_database_url


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(sqlite_database_url(tmp_path / "test.db"))
    value.create_schema()
    yield value
    value.dispose()
