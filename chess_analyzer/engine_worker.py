"""
engine_worker.py
Runs Stockfish in a background thread. Evaluates all legal moves for a given
piece, caches results, and delivers them back to the main thread via a callback.
"""

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import chess
import chess.engine


@dataclass
class EvalRequest:
    request_id: int
    board: chess.Board
    moves: list  # list[chess.Move]
    callback: Callable  # called on main thread with EvalResult
    depth: int = 15
    progress_callback: Optional[Callable] = None  # fired after each move with partial EvalResult


@dataclass
class EvalResult:
    request_id: int
    move_evals: Dict[str, chess.engine.Score]  # uci string → Score
    board_fen: str  # fen the eval was computed on


class EngineWorker:
    def __init__(self, stockfish_path: Path, root_widget, depth: int = 15) -> None:
        self._path = stockfish_path
        self._root = root_widget  # tk root, used for root.after()
        self._depth = depth
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._cache: Dict[tuple, chess.engine.Score] = {}  # (fen, uci) → Score
        self._latest_request_id: int = 0
        self._engine: Optional[chess.engine.SimpleEngine] = None
        self._lock = threading.Lock()

        # Dedicated engine + serialisation lock for multi-PV analysis so it
        # never shares the heatmap engine across threads.
        self._multipv_engine: Optional[chess.engine.SimpleEngine] = None
        self._multipv_lock = threading.Lock()   # one multipv analysis at a time
        self._multipv_req_id: int = 0           # incremented on each new request

        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    def submit_request(self, request: EvalRequest) -> None:
        """Submit an eval request. Discards any pending (unstarted) request."""
        with self._lock:
            self._latest_request_id = request.request_id
        # Drain any queued-but-not-started request
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(request)
        except queue.Full:
            pass  # worker just grabbed one; next hover will re-submit

    def set_depth(self, depth: int) -> None:
        self._depth = depth

    def submit_position_analysis(
        self, board: chess.Board, depth: int, multipv: int, callback: Callable
    ) -> None:
        """Analyse the current position for the top N lines (multi-PV).
        Uses a dedicated engine so it never races with the heatmap thread."""
        with self._lock:
            self._multipv_req_id += 1
            req_id = self._multipv_req_id
        threading.Thread(
            target=self._run_multipv,
            args=(board.copy(), depth, multipv, callback, req_id),
            daemon=True,
        ).start()

    def _run_multipv(
        self, board: chess.Board, depth: int, multipv: int, callback: Callable,
        req_id: int,
    ) -> None:
        # Serialise access to the dedicated multipv engine; if we're already
        # stale by the time we acquire the lock, bail out immediately.
        with self._multipv_lock:
            with self._lock:
                if req_id != self._multipv_req_id:
                    return  # superseded before we even started
            if self._multipv_engine is None:
                return
            try:
                infos = self._multipv_engine.analyse(
                    board, chess.engine.Limit(depth=depth), multipv=multipv
                )
            except Exception:
                return
            # Check freshness again after the (potentially slow) analyse call
            with self._lock:
                if req_id != self._multipv_req_id:
                    return  # a newer request arrived while we were analysing
        if not isinstance(infos, list):
            infos = [infos]
        lines: List[dict] = []
        for info in infos:
            pv = info.get("pv", [])
            score = info.get("score")
            if pv:
                san = board.san(pv[0])
                cp = score.white().score(mate_score=30000) if score else None
                lines.append({"san": san, "cp": cp, "pv_uci": [m.uci() for m in pv[:6]]})
        self._root.after(0, lambda l=lines: callback(l))

    def shutdown(self) -> None:
        """Graceful shutdown: stop worker thread then quit both engines."""
        self._queue.put(None)  # sentinel
        self._thread.join(timeout=6)
        for eng in (self._engine, self._multipv_engine):
            if eng:
                try:
                    eng.quit()
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _worker_loop(self) -> None:
        self._engine = chess.engine.SimpleEngine.popen_uci(str(self._path))
        self._multipv_engine = chess.engine.SimpleEngine.popen_uci(str(self._path))

        while True:
            request = self._queue.get()
            if request is None:
                break  # shutdown sentinel

            with self._lock:
                if request.request_id < self._latest_request_id:
                    continue  # already superseded

            move_evals = self._evaluate_moves(request)

            with self._lock:
                is_fresh = request.request_id == self._latest_request_id

            if is_fresh:
                result = EvalResult(
                    request_id=request.request_id,
                    move_evals=move_evals,
                    board_fen=request.board.fen(),
                )
                self._root.after(0, lambda r=result, cb=request.callback: cb(r))

        # Worker thread ending — engine quit happens in shutdown()

    def _evaluate_moves(self, request: EvalRequest) -> Dict[str, chess.engine.Score]:
        results: Dict[str, chess.engine.Score] = {}
        base_fen = request.board.fen()

        for move in request.moves:
            # Check staleness mid-loop
            with self._lock:
                if request.request_id < self._latest_request_id:
                    break

            cache_key = (base_fen, move.uci())
            if cache_key in self._cache:
                results[move.uci()] = self._cache[cache_key]
                continue

            # Push move, analyse resulting position, pop move
            board_copy = request.board.copy()
            board_copy.push(move)
            try:
                info = self._engine.analyse(
                    board_copy,
                    chess.engine.Limit(depth=request.depth),
                )
                score = info["score"]
                self._cache[cache_key] = score
                results[move.uci()] = score
                if request.progress_callback:
                    partial = EvalResult(
                        request_id=request.request_id,
                        move_evals=dict(results),
                        board_fen=base_fen,
                    )
                    self._root.after(
                        0, lambda r=partial, cb=request.progress_callback: cb(r)
                    )
            except chess.engine.EngineTerminatedError:
                break
            except Exception:
                pass

        return results
