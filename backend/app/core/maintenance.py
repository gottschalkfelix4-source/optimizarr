"""Coordinate short exclusive file maintenance with scanner/queue dispatch."""
from contextlib import contextmanager
from functools import wraps
import threading

lock = threading.RLock()
active = False


@contextmanager
def exclusive():
    global active
    with lock:
        if active:
            raise ValueError("Eine Wiederherstellung laeuft bereits.")
        active = True
    try:
        yield
    finally:
        with lock:
            active = False


def claim_guard(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with lock:
            return [] if active else fn(*args, **kwargs)
    return guarded


def enqueue_guard(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with lock:
            if active:
                return 0, ["Wiederherstellung laeuft; bitte danach erneut einreihen."]
            return fn(*args, **kwargs)
    return guarded
