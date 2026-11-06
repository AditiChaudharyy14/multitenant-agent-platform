"""Server-generated ids fed back to the model must be reproducible under evals."""

import uuid

from app.agents.ids import deterministic_ids, new_id


def test_default_ids_are_random_uuid4() -> None:
    a, b = new_id(), new_id()
    assert a != b and a.version == 4


def test_deterministic_ids_repeat_for_the_same_seed() -> None:
    with deterministic_ids("store/reorder"):
        first = [new_id() for _ in range(3)]
    with deterministic_ids("store/reorder"):
        second = [new_id() for _ in range(3)]
    with deterministic_ids("store/other"):
        other = new_id()
    assert first == second
    assert len(set(first)) == 3
    assert other not in first
    assert all(isinstance(i, uuid.UUID) for i in first)


def test_deterministic_scope_does_not_leak() -> None:
    with deterministic_ids("x"):
        pass
    assert new_id().version == 4
