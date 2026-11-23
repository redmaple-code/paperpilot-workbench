"""Request-local I/O deadlines, shared by nested Agent calls without global env edits."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline = ContextVar('agent_io_deadline', default=None)


def timeout(configured: float) -> float:
    deadline = _deadline.get()
    remaining = deadline - time.monotonic() if deadline is not None else float(configured)
    if remaining <= 0:
        raise TimeoutError('Agent I/O deadline exceeded')
    return max(.01, min(float(configured), remaining))


def expired() -> bool:
    value = _deadline.get()
    return value is not None and time.monotonic() >= value


@contextmanager
def deadline(seconds: float):
    prior = _deadline.get()
    value = time.monotonic() + seconds
    token = _deadline.set(min(prior, value) if prior is not None else value)
    try:
        yield
    finally:
        _deadline.reset(token)
