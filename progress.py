"""Progress reports and cancellation for the planner's slow steps.

Downloads, graph building and the route search call report() and check() as
they go. Outside the web app nothing is listening, so these do nothing and the
command line behaves as before. The web app runs each search inside track(),
which passes the reports on to the page's progress view and lets the search be
cancelled part-way through.

    with progress.track(on_report, cancel_event):
        workspace.plan(request)      # raises progress.Cancelled if cancel_event is set
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

_local = threading.local()


class Cancelled(Exception):
    """The search was cancelled while it ran."""


@contextmanager
def track(on_report=None, cancel: threading.Event | None = None):
    """Route this thread's progress reports to on_report(stage, done, total), cancellable by `cancel`."""
    previous = getattr(_local, "ctx", None)
    _local.ctx = (on_report, cancel)
    try:
        yield
    finally:
        _local.ctx = previous


def report(stage: str, done: float | None = None, total: float | None = None) -> None:
    """Say which step is running and, when known, how far through it is (done of total)."""
    ctx = getattr(_local, "ctx", None)
    if ctx and ctx[0]:
        ctx[0](stage, done, total)


def cancelled() -> bool:
    ctx = getattr(_local, "ctx", None)
    return bool(ctx and ctx[1] is not None and ctx[1].is_set())


def check() -> None:
    """Stop here, by raising Cancelled, if the search has been cancelled."""
    if cancelled():
        raise Cancelled()


def sleep(seconds: float) -> None:
    """time.sleep() that ends early, raising Cancelled, when the search is cancelled."""
    ctx = getattr(_local, "ctx", None)
    event = ctx[1] if ctx else None
    if event is None:
        time.sleep(seconds)
    elif event.wait(seconds):
        raise Cancelled()
