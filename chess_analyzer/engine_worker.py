"""One Stockfish process, one background thread, three kinds of request.

``HOVER`` (the user is waiting for the evals of one piece), ``HEATMAP`` (every legal move of
the shown position) and ``BATCH`` (Analyze Game, in the background). Higher-priority work
preempts lower-priority work between moves and mid-search; a preempted request goes back to
its slot and resumes from the evaluation cache. Results reach the UI through a
``UiDispatcher``, so nothing here touches Tk and shutdown cannot deadlock on the main loop.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol

import chess
import chess.engine
from chess.engine import PovScore

from .ui_dispatch import UiDispatcher

log = logging.getLogger(__name__)


class Kind(IntEnum):
    """Request kinds; a lower value preempts a higher one."""

    HOVER = 0
    HEATMAP = 1
    BATCH = 2


@dataclass(frozen=True)
class MoveEval:
    """Evaluation of the position reached after a move."""

    score: PovScore  # relative to the side to move *after* the move (python-chess style)
    pv: tuple[chess.Move, ...]  # the engine's continuation after the move
    depth: int


@dataclass(frozen=True)
class EvalRequest:
    kind: Kind
    board: chess.Board  # the position; the caller passes a copy
    moves: tuple[chess.Move, ...]  # moves to evaluate, in priority order
    depth: int
    on_done: Callable[[EvalResult], None]
    on_progress: Callable[[EvalResult], None] | None = None
    tag: Any = None  # returned untouched (Analyze Game uses the ply index)


@dataclass(frozen=True)
class EvalResult:
    request_id: int
    kind: Kind
    epd: str  # consumers must compare this with the position they are showing
    move_evals: Mapping[str, MoveEval]  # uci -> evaluation
    complete: bool  # False: the engine failed; the evaluations are partial
    tag: Any = None


class EngineLike(Protocol):
    """The slice of ``chess.engine.SimpleEngine`` the worker uses (tests pass a fake)."""

    id: Mapping[str, str]
    options: Mapping[str, Any]

    def configure(self, options: Mapping[str, Any]) -> None: ...
    def analysis(self, board: chess.Board, limit: chess.engine.Limit) -> Any: ...
    def quit(self) -> None: ...
    def close(self) -> None: ...


def open_engine(path: Path, timeout: float = 30.0) -> chess.engine.SimpleEngine:
    """Start a UCI engine; the generous timeout covers antivirus scans of a fresh binary."""
    return chess.engine.SimpleEngine.popen_uci(str(path), timeout=timeout)


@dataclass
class _Pending:
    id: int
    request: EvalRequest
    interrupted: bool = False  # a higher-priority request asked the search to stop


class EngineWorker:
    def __init__(
        self,
        engine_path: Path | str,
        dispatcher: UiDispatcher,
        *,
        threads: int = 0,
        hash_mb: int = 256,
        on_error: Callable[[str], None] | None = None,
        engine_factory: Callable[[Path], EngineLike] | None = None,
        cache_size: int = 20_000,
    ) -> None:
        self._path = Path(engine_path)
        self._dispatcher = dispatcher
        self._threads = threads or max(1, (os.cpu_count() or 2) // 2)
        self._hash_mb = hash_mb
        self._on_error = on_error
        self._factory = engine_factory or open_engine
        self._cache: OrderedDict[tuple[str, str, int], MoveEval] = OrderedDict()
        self._cache_size = cache_size

        self._cond = threading.Condition()
        self._slots: dict[Kind, _Pending | None] = {Kind.HOVER: None, Kind.HEATMAP: None}
        self._batch: deque[_Pending] = deque()
        self._cancelled: set[int] = set()
        self._current: _Pending | None = None
        self._current_analysis: Any = None
        self._next_id = 1
        self._stop = threading.Event()

        self._engine: EngineLike | None = None
        self._ever_started = False
        self._failures = 0  # consecutive engine failures; reset by a successful evaluation
        self._dead = False  # the engine failed and could not be restarted

        self._thread = threading.Thread(target=self._run, name="engine-worker", daemon=True)
        self._thread.start()

    # ── Public API (any thread) ───────────────────────────────────────────────

    def submit(self, request: EvalRequest) -> int:
        """Queue a request and return its id.

        HOVER and HEATMAP requests replace any pending request of the same kind (and cancel
        one in flight); BATCH requests queue in order.
        """
        with self._cond:
            rid = self._next_id
            self._next_id += 1
            pending = _Pending(rid, request)
            if request.kind is Kind.BATCH:
                self._batch.append(pending)
            else:
                old = self._slots[request.kind]
                if old is not None:
                    self._cancelled.add(old.id)
                self._slots[request.kind] = pending
                current = self._current
                if current is not None:
                    if current.request.kind == request.kind:
                        self._cancelled.add(current.id)
                        self._interrupt_locked()
                    elif current.request.kind > request.kind:
                        current.interrupted = True
                        self._interrupt_locked()
            self._cond.notify_all()
        return rid

    def cancel(self, request_id: int) -> None:
        with self._cond:
            for kind, pending in self._slots.items():
                if pending is not None and pending.id == request_id:
                    self._slots[kind] = None
                    return
            for pending in list(self._batch):
                if pending.id == request_id:
                    self._batch.remove(pending)
                    return
            if self._current is not None and self._current.id == request_id:
                self._cancelled.add(request_id)
                self._interrupt_locked()

    def cancel_kind(self, kind: Kind) -> None:
        """Drop every pending request of a kind and abort one in flight."""
        with self._cond:
            if kind is Kind.BATCH:
                self._batch.clear()
            else:
                self._slots[kind] = None
            if self._current is not None and self._current.request.kind == kind:
                self._cancelled.add(self._current.id)
                self._interrupt_locked()

    @property
    def alive(self) -> bool:
        return not self._dead

    def shutdown(self, timeout: float = 3.0) -> None:
        """Stop the worker and quit the engine; never blocks longer than ``timeout``."""
        self._stop.set()
        with self._cond:
            if self._current is not None:
                self._cancelled.add(self._current.id)
            self._interrupt_locked()
            self._cond.notify_all()
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("Engine thread still busy after %.1fs; closing the engine", timeout)
            engine = self._engine
            if engine is not None:
                try:
                    engine.close()
                except Exception:
                    log.debug("engine.close() failed", exc_info=True)

    # ── Worker thread ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            pending = self._take_next()
            if pending is None:
                break
            try:
                self._process(pending)
            except Exception:
                log.exception("Unexpected error while evaluating")
                self._finish(pending, {}, complete=False)
            finally:
                with self._cond:
                    self._current = None
        self._close_engine()

    def _take_next(self) -> _Pending | None:
        with self._cond:
            while not self._stop.is_set():
                for kind in (Kind.HOVER, Kind.HEATMAP):
                    pending = self._slots[kind]
                    if pending is not None:
                        self._slots[kind] = None
                        self._current = pending
                        return pending
                if self._batch:
                    self._current = self._batch.popleft()
                    return self._current
                self._cond.wait()
            return None

    def _process(self, pending: _Pending) -> None:
        request = pending.request
        results: dict[str, MoveEval] = {}
        for move in request.moves:
            if self._is_cancelled(pending):
                return
            if self._must_yield(pending):
                self._requeue(pending)
                return
            move_eval, engine_ok = self._evaluate_with_retry(pending, move)
            if not engine_ok:
                self._finish(pending, results, complete=False)
                return
            if move_eval is None:  # the search was interrupted
                if self._is_cancelled(pending) or self._stop.is_set():
                    return
                self._requeue(pending)
                return
            results[move.uci()] = move_eval
            if request.on_progress is not None:
                self._post(request.on_progress, self._result(pending, results, complete=False))
        self._finish(pending, results, complete=True)

    def _evaluate_with_retry(
        self, pending: _Pending, move: chess.Move
    ) -> tuple[MoveEval | None, bool]:
        """Evaluate one move, restarting the engine once if it dies. (eval, engine usable)."""
        for attempt in (1, 2):
            engine = self._ensure_engine()
            if engine is None:
                return None, False
            try:
                move_eval = self._evaluate(engine, pending, move)
            except (chess.engine.EngineError, OSError, TimeoutError) as exc:
                log.warning("Engine failed (attempt %d): %s", attempt, exc)
                self._discard_engine()
                continue
            self._failures = 0
            return move_eval, True
        self._give_up("The chess engine keeps crashing; analysis is disabled for this session.")
        return None, False

    def _evaluate(self, engine: EngineLike, pending: _Pending, move: chess.Move) -> MoveEval | None:
        """Evaluate the position after ``move``; None if the search was interrupted."""
        request = pending.request
        key = (request.board.epd(), move.uci(), request.depth)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        after = request.board.copy(stack=False)
        after.push(move)
        limit = chess.engine.Limit(depth=request.depth)
        aborted = False
        with engine.analysis(after, limit) as analysis:
            with self._cond:
                self._current_analysis = analysis
            try:
                for _info in analysis:
                    if self._should_abort(pending):
                        aborted = True
                        analysis.stop()
                        break
            finally:
                with self._cond:
                    self._current_analysis = None
        with self._cond:
            if aborted or pending.interrupted or pending.id in self._cancelled:
                return None
        info = analysis.info
        score = info.get("score") or _terminal_score(after)
        if score is None:
            raise chess.engine.EngineError(f"no score reported for {after.fen()}")
        move_eval = MoveEval(
            score=score, pv=tuple(info.get("pv", ())), depth=int(info.get("depth", 0))
        )
        self._cache[key] = move_eval
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return move_eval

    # ── Scheduling helpers ────────────────────────────────────────────────────

    def _interrupt_locked(self) -> None:
        analysis = self._current_analysis
        if analysis is not None:
            try:
                analysis.stop()
            except Exception:
                log.debug("analysis.stop() failed", exc_info=True)

    def _higher_priority_waiting_locked(self, kind: Kind) -> bool:
        return any(self._slots[k] is not None for k in self._slots if k < kind)

    def _should_abort(self, pending: _Pending) -> bool:
        if self._stop.is_set():
            return True
        with self._cond:
            return (
                pending.interrupted
                or pending.id in self._cancelled
                or self._higher_priority_waiting_locked(pending.request.kind)
            )

    def _is_cancelled(self, pending: _Pending) -> bool:
        with self._cond:
            return pending.id in self._cancelled

    def _must_yield(self, pending: _Pending) -> bool:
        with self._cond:
            return self._higher_priority_waiting_locked(pending.request.kind)

    def _requeue(self, pending: _Pending) -> None:
        """Put an interrupted request back so it resumes once higher-priority work is done."""
        with self._cond:
            pending.interrupted = False
            kind = pending.request.kind
            if kind is Kind.BATCH:
                self._batch.appendleft(pending)
            elif self._slots[kind] is None:
                self._slots[kind] = pending
            # else: a newer request of this kind has replaced it

    # ── Engine lifecycle ──────────────────────────────────────────────────────

    def _ensure_engine(self) -> EngineLike | None:
        if self._engine is not None:
            return self._engine
        if self._dead:
            return None
        if self._failures > 1:
            self._give_up("The chess engine keeps crashing; analysis is disabled.")
            return None
        try:
            engine = self._factory(self._path)
        except Exception as exc:
            log.error("Could not start engine %s: %s", self._path, exc)
            self._failures += 1
            if not self._ever_started or self._failures > 1:
                self._give_up(f"Could not start the chess engine:\n{exc}")
            return None
        self._configure(engine)
        self._engine = engine
        self._ever_started = True
        log.info(
            "Engine started: %s (Threads=%d, Hash=%d MB)",
            engine.id.get("name", "?"),
            self._threads,
            self._hash_mb,
        )
        return engine

    def _configure(self, engine: EngineLike) -> None:
        options: dict[str, Any] = {}
        if "Threads" in engine.options:
            options["Threads"] = self._threads
        if "Hash" in engine.options:
            options["Hash"] = self._hash_mb
        if options:
            try:
                engine.configure(options)
            except Exception as exc:
                log.warning("Could not configure engine options %s: %s", options, exc)

    def _discard_engine(self) -> None:
        engine, self._engine = self._engine, None
        self._failures += 1
        if engine is not None:
            try:
                engine.close()
            except Exception:
                log.debug("engine.close() failed", exc_info=True)

    def _close_engine(self) -> None:
        engine, self._engine = self._engine, None
        if engine is None:
            return
        try:
            engine.quit()
        except Exception as exc:
            log.debug("engine.quit() failed (%s); closing instead", exc)
            try:
                engine.close()
            except Exception:
                log.debug("engine.close() failed", exc_info=True)

    def _give_up(self, message: str) -> None:
        if self._dead:
            return
        self._dead = True
        log.error(message)
        if self._on_error is not None:
            self._dispatcher.post(self._on_error, message)

    # ── Results ───────────────────────────────────────────────────────────────

    def _result(
        self, pending: _Pending, results: dict[str, MoveEval], complete: bool
    ) -> EvalResult:
        request = pending.request
        return EvalResult(
            request_id=pending.id,
            kind=request.kind,
            epd=request.board.epd(),
            move_evals=dict(results),
            complete=complete,
            tag=request.tag,
        )

    def _finish(self, pending: _Pending, results: dict[str, MoveEval], complete: bool) -> None:
        self._post(pending.request.on_done, self._result(pending, results, complete))

    def _post(self, fn: Callable[[EvalResult], None], result: EvalResult) -> None:
        self._dispatcher.post(fn, result)


def _terminal_score(board: chess.Board) -> PovScore | None:
    """Score for a position with no legal moves, should the engine not report one."""
    if board.is_checkmate():
        return PovScore(chess.engine.Mate(0), board.turn)  # Mate(-0): side to move is mated
    if board.is_game_over():
        return PovScore(chess.engine.Cp(0), board.turn)
    return None
