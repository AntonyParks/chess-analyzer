"""Qt implementation of the ``Dispatcher`` protocol: a queued signal into the GUI thread."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot

from ..ui_dispatch import run_job


class QtDispatcher(QObject):
    """Create it on the GUI thread; ``post`` may then be called from any thread."""

    _posted = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._closed = False
        self._posted.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def post(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> bool:
        if self._closed:
            return False
        self._posted.emit((fn, args, kwargs))
        return True

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def drain(self) -> None:
        """Process queued jobs now (GUI thread only; handy in tests)."""
        QCoreApplication.processEvents()

    @Slot(object)
    def _run(self, job: tuple) -> None:
        if self._closed:
            return
        fn, args, kwargs = job
        run_job(fn, args, kwargs)
