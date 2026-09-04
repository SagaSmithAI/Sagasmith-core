from __future__ import annotations

from threading import Event, Thread

import pytest

from sagasmith_core import CharacterService
from sagasmith_core.characters import CharacterNotFoundError
from sagasmith_core.models import Character


def test_get_for_update_reads_in_ambient_transaction(database) -> None:
    character = CharacterService(database).create(system_id="dnd5e", name="Locked")

    with database.transaction(immediate=True):
        locked = CharacterService(database).get_for_update(character.id)

    assert locked.id == character.id
    assert locked.name == "Locked"
    assert locked.revision == character.revision


def test_get_for_update_raises_for_missing_character(database) -> None:
    with pytest.raises(CharacterNotFoundError):
        CharacterService(database).get_for_update("missing-character")


def test_get_for_update_fails_closed_inside_deferred_ambient_transaction(database) -> None:
    character = CharacterService(database).create(system_id="dnd5e", name="Deferred")
    with database.transaction(), pytest.raises(RuntimeError, match="immediate transaction"):
        CharacterService(database).get_for_update(character.id)


def test_sqlite_get_for_update_serializes_competing_transactions(database) -> None:
    character = CharacterService(database).create(system_id="dnd5e", name="Before")
    second_attempting = Event()
    first_acquired = Event()
    result: list[str] = []
    errors: list[BaseException] = []

    def second() -> None:
        try:
            second_attempting.set()
            locked = CharacterService(database).get_for_update(character.id)
            result.append(locked.name)
        except BaseException as error:  # pragma: no cover - surfaced below
            errors.append(error)

    worker = Thread(target=second)
    with database.transaction(immediate=True) as session:
        CharacterService(database).get_for_update(character.id)
        first_acquired.set()
        worker.start()
        assert second_attempting.wait(timeout=1)
        # CharacterInfo is intentionally detached; update the canonical row
        # through the ambient ORM session held by this transaction.
        locked_row = session.get(Character, character.id)
        assert locked_row is not None
        locked_row.name = "After"
    worker.join(timeout=5)

    assert first_acquired.is_set()
    assert not worker.is_alive()
    assert errors == []
    assert result == ["After"]
