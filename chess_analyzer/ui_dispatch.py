"""Marshal work from background threads onto the Tk main thread.

Tk is not thread-safe, and ``root.after`` called from another thread blocks that
thread until the main loop services the call — which is how the old close-window
hang happened. Background threads therefore hand callables to a ``UiDispatcher``,
which runs them from the main thread on a short timer.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

_Job = tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]


class UiDispatcher:
    def __init__(self, root: Any, interval_ms: int = 15, max_per_tick: int = 200) -> None:
        self._root = root  # anything with after()/after_cancel(); duck-typed for tests
        self._interval = interval_ms
        self._max_per_tick = max_per_tick
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._after_id: str | None = None
        self._closed = threading.Event()

    def start(self) -> None:
        if self._after_id is None and not self._closed.is_set():
            self._after_id = self._root.after(self._interval, self._pump)

    def post(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> bool:
        """Queue ``fn(*args, **kwargs)`` for the main thread. Returns False after close()."""
        if self._closed.is_set():
            return False
        self._queue.put((fn, args, kwargs))
        return True

    def drain(self) -> int:
        """Run everything queued right now (main thread only). Returns the count run."""
        ran = 0
        while not self._closed.is_set():
            try:
                fn, args, kwargs = self._queue.get_nowait()
            except queue.Empty:
                break
            self._run(fn, args, kwargs)
            ran += 1
        return ran

    def close(self) -> None:
        """Stop pumping and drop queued work. Later posts are ignored."""
        self._closed.set()
        if self._after_id is not None:
            try:
                self._root.after_cancel(self._after_id)
            except Exception:  # Tk already torn down
                log.debug("after_cancel failed during close", exc_info=True)
            self._after_id = None
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _pump(self) -> None:
        self._after_id = None
        if self._closed.is_set():
            return
        for _ in range(self._max_per_tick):
            try:
                fn, args, kwargs = self._queue.get_nowait()
            except queue.Empty:
                break
            self._run(fn, args, kwargs)
            if self._closed.is_set():  # a callback closed us (e.g. the window was destroyed)
                return
        self._after_id = self._root.after(self._interval, self._pump)

    @staticmethod
    def _run(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("Error in UI callback %r", fn)
