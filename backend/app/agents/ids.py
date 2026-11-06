"""Ids the server generates and feeds back to the model (proposal ids, later).

Under EVAL_MODE=replay every request must match its recording byte for byte, so these ids
must be reproducible: the eval runner wraps each scenario in deterministic_ids(name).
Everywhere else new_id() is a plain uuid4.
"""

import itertools
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_NAMESPACE = uuid.UUID("6f6c6b9e-5b0e-4c43-9a51-6d0a2b7e1c11")  # fixed: ids stable across runs

_factory: ContextVar[Callable[[], uuid.UUID]] = ContextVar("id_factory", default=uuid.uuid4)


def new_id() -> uuid.UUID:
    return _factory.get()()


@contextmanager
def deterministic_ids(seed: str) -> Iterator[None]:
    """Inside this block new_id() returns uuid5(seed:0), uuid5(seed:1), …"""
    counter = itertools.count()
    token = _factory.set(lambda: uuid.uuid5(_NAMESPACE, f"{seed}:{next(counter)}"))
    try:
        yield
    finally:
        _factory.reset(token)
