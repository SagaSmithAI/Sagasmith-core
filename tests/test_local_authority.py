import subprocess
import sys

import pytest

from sagasmith_core.database import Database, sqlite_database_url


def test_shared_connections_and_exclusive_local_owner(tmp_path):
    url = sqlite_database_url(tmp_path / "save.db")
    first, second = Database(url), Database(url)
    try:
        with pytest.raises(RuntimeError, match="incompatible authority"):
            Database(url, local_authority=True)
    finally:
        first.dispose()
        second.dispose()
    owner = Database(url, local_authority=True)
    try:
        program = "from sagasmith_core.database import Database; import sys; Database(sys.argv[1])"
        result = subprocess.run(
            [sys.executable, "-c", program, url], capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "incompatible authority" in result.stderr
    finally:
        owner.dispose()
    reopened = Database(url, local_authority=True)
    reopened.dispose()
    with pytest.raises(RuntimeError, match="authority was released"):
        with owner.engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")


def test_process_death_releases_ownership(tmp_path):
    url = sqlite_database_url(tmp_path / "save.db")
    program = (
        "from sagasmith_core.database import Database; import sys, os; "
        "d=Database(sys.argv[1], local_authority=True); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", program, url], check=True)
    owner = Database(url, local_authority=True)
    owner.dispose()
