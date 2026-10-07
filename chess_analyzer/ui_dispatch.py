"""How background threads reach the UI thread.

GUI toolkits are not thread-safe. Every background thread in the app (engine worker,
Lichess fetches, Opening Insights, engine setup) hands callables to a ``Dispatcher`` and the
UI runs them on its own thread. The Qt UI provides ``chess_analyzer.ui.dispatcher.QtDispatcher``;
``QueueDispatcher`` here is the toolkit-free version used by tests and scripts.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

_Job = tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]


class Dispatcher(Protocol):
    """Thread-safe hand-off of callables to the UI thread."""

    def post(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> bool: ...

    def close(self) -> None: ...

    @property
    def closed(self) -> bool: ...


class QueueDispatcher:
    """Queues posted jobs; whoever owns the "UI thread" calls ``drain()`` to run them."""

    def __init__(self) -> None:
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._closed = threading.Event()

    def post(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> bool:
        """Queue ``fn(*args, **kwargs)``. Returns False after close()."""
        if self._closed.is_set():
            return False
        self._queue.put((fn, args, kwargs))
        return True

    def drain(self) -> int:
        """Run everything queued right now. Returns the count run."""
        ran = 0
        while not self._closed.is_set():
            try:
                fn, args, kwargs = self._queue.get_nowait()
            except queue.Empty:
                break
            run_job(fn, args, kwargs)
            ran += 1
        return ran

    def close(self) -> None:
        """Drop queued work; later posts are ignored."""
        self._closed.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    @property
    def closed(self) -> bool:
        return self._closed.is_set()


def run_job(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Run one posted callable, logging (not propagating) anything it raises."""
    try:
        fn(*args, **kwargs)
    except Exception:
        log.exception("Error in UI callback %r", fn)
