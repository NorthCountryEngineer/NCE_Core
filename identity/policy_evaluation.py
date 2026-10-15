"""Facts reused only while producing one live authorization response."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class _Evaluation:
    active: bool = True
    facts: dict = field(default_factory=dict)


_evaluation = ContextVar("core_policy_evaluation", default=None)


def evaluation_active():
    current = _evaluation.get()
    return bool(current is not None and current.active)


@contextmanager
def policy_evaluation():
    """Nested helpers share facts; later requests and copied contexts cannot."""
    current = _evaluation.get()
    if current is not None and current.active:
        yield
        return
    current = _Evaluation()
    token = _evaluation.set(current)
    try:
        yield
    finally:
        current.active = False
        current.facts.clear()
        _evaluation.reset(token)


def policy_fact(key, load):
    current = _evaluation.get()
    if current is None or not current.active:
        return load()
    if key not in current.facts:
        current.facts[key] = load()
    return current.facts[key]
