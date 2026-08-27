"""
player_analysis.py
BFS traversal of the Lichess opening tree to find statistically notable moves:
positions where a move's win rate significantly exceeds the weighted average
win rate of all moves in that position.
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional

import chess


@dataclass
class NotableMove:
    fen: str                       # FEN of the position BEFORE the notable move
    move_uci_sequence: List[str]   # UCI moves from start to reach this position
    san: str                       # SAN of the notable move
    uci: str                       # UCI of the notable move
    move_wr: float                 # win rate of this move 0–100
    pos_avg_wr: float              # weighted avg win rate across all moves at this position
    delta: float                   # move_wr - pos_avg_wr (percentage points)
    popularity_rank: int           # 1 = most-played move in this position


class OpeningAnalysisWorker:
    """
    Background BFS worker that scans the Lichess opening tree from a given
    starting position, flagging moves whose statistical win rate exceeds the
    weighted average for that position by at least delta_threshold pp.

    Rate: 1 request/sec.
    Pruning: a branch is cut when total games at that node fall below
    min_pct % of the game count when entering via the parent move.
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._cache: dict = {}   # (fen, speeds_key, ratings_key) -> data

    # ── Public interface ──────────────────────────────────────────────────────

    def start(
        self,
        color: str,                  # "white" or "black" — whose win rate to track
        min_pct: int,                # 1–50 — minimum games as % of parent move's games
        min_games: int,              # absolute minimum games for a move to be flagged/explored
        top_n: int,                  # 0 = all moves; >0 = only top N by game count
        speed_filters: List[str],    # e.g. ["bullet", "blitz", "rapid"]
        rating_filters: List[int],   # e.g. [1600, 1800, 2000, 2200]
        delta_threshold: float,      # minimum move_wr - pos_avg_wr to flag (pp)
        start_fen: str,              # FEN to begin BFS from
        token: str,
        on_progress: Callable[[str], None],
        on_result: Callable[["NotableMove"], None],
        on_complete: Callable[[int], None],
        root,                        # tk.Tk — for root.after thread-marshalling
    ) -> None:
        self._stop_event.clear()
        t = threading.Thread(
            target=self._run,
            args=(color, min_pct, min_games, top_n, speed_filters, rating_filters, delta_threshold, start_fen,
                  token, on_progress, on_result, on_complete, root),
            daemon=True,
        )
        t.start()

    def stop(self) -> None:
        self._stop_event.set()

    # ── BFS worker ────────────────────────────────────────────────────────────

    def _run(
        self,
        color: str,
        min_pct: int,
        min_games: int,
        top_n: int,
        speed_filters: List[str],
        rating_filters: List[int],
        delta_threshold: float,
        start_fen: str,
        token: str,
        on_progress: Callable,
        on_result: Callable,
        on_complete: Callable,
        root,
    ) -> None:
        speeds_key = ",".join(sorted(speed_filters))
        ratings_key = ",".join(str(r) for r in sorted(rating_filters))
        headers: dict = {"User-Agent": "ChessAnalyzerApp/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # BFS queue items: (fen, move_uci_sequence, parent_move_games)
        # parent_move_games=None at root — skip prune check for the first node.
        bfs: deque = deque()
        bfs.append((start_fen, [], None))

        positions_checked = 0
        last_request_time = 0.0

        while bfs and not self._stop_event.is_set():
            fen, seq, parent_games = bfs.popleft()
            positions_checked += 1
            _n = positions_checked
            root.after(0, lambda n=_n: on_progress(f"Analyzing... {n} positions checked"))

            # ── Rate limit + fetch ────────────────────────────────────────────
            last_request_time = self._rate_limit(last_request_time)
            if self._stop_event.is_set():
                break

            data = self._fetch(fen, speeds_key, ratings_key, headers)
            last_request_time = time.time()
            if data is None:
                continue

            moves = data.get("moves", [])
            if not moves:
                continue

            # Restrict opponent's responses to top N — player's own moves are fully explored
            active = fen.split()[1]  # 'w' or 'b'
            is_opponent_turn = (active == "w") != (color == "white")
            if top_n > 0 and is_opponent_turn:
                moves = sorted(
                    moves,
                    key=lambda m: m.get("white", 0) + m.get("draws", 0) + m.get("black", 0),
                    reverse=True,
                )[:top_n]

            # Total games across all moves at this position
            position_total = sum(
                m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
                for m in moves
            )
            if position_total == 0:
                continue

            # Prune: skip branch if too few games reached via the parent move
            if parent_games is not None and parent_games > 0:
                if position_total < (min_pct / 100.0) * parent_games:
                    continue

            # Weighted average win rate for this position
            pos_avg_wr = self._weighted_avg_wr(moves, color)

            # Sort by game count to assign popularity ranks
            sorted_by_games = sorted(
                moves,
                key=lambda m: m.get("white", 0) + m.get("draws", 0) + m.get("black", 0),
                reverse=True,
            )
            rank_map = {m.get("uci", ""): i + 1 for i, m in enumerate(sorted_by_games)}

            board = chess.Board(fen)

            for move_data in moves:
                if self._stop_event.is_set():
                    break

                uci = move_data.get("uci", "")
                if not uci:
                    continue

                pw = move_data.get("white", 0)
                pd = move_data.get("draws", 0)
                pb = move_data.get("black", 0)
                pt = pw + pd + pb
                if pt == 0:
                    continue
                if min_games > 0 and pt < min_games:
                    continue

                move_wr = 100.0 * (pw if color == "white" else pb) / pt
                delta = move_wr - pos_avg_wr

                if delta >= delta_threshold:
                    try:
                        move = chess.Move.from_uci(uci)
                        san = board.san(move)
                    except Exception:
                        continue

                    nm = NotableMove(
                        fen=fen,
                        move_uci_sequence=list(seq),
                        san=san,
                        uci=uci,
                        move_wr=round(move_wr, 1),
                        pos_avg_wr=round(pos_avg_wr, 1),
                        delta=round(delta, 1),
                        popularity_rank=rank_map.get(uci, 0),
                    )
                    root.after(0, lambda r=nm: on_result(r))

                # Always enqueue child so we explore the full sub-tree.
                # Use this move's game count as the parent_games for the child,
                # so pruning is relative to how many games reached THIS line.
                try:
                    child_board = board.copy()
                    child_board.push(chess.Move.from_uci(uci))
                    bfs.append((child_board.fen(), seq + [uci], pt))
                except Exception:
                    pass

        _total = positions_checked
        root.after(0, lambda n=_total: on_complete(n))

    # ── API helpers ───────────────────────────────────────────────────────────

    def _rate_limit(self, last_time: float) -> float:
        elapsed = time.time() - last_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        return time.time()

    def _fetch(self, fen: str, speeds_key: str, ratings_key: str, headers: dict) -> Optional[dict]:
        cache_key = (fen, speeds_key, ratings_key)
        if cache_key in self._cache:
            return self._cache[cache_key]

        params = [
            ("fen", fen),
            ("topGames", 0),
            ("recentGames", 0),
            ("variant", "standard"),
        ]
        if speeds_key:
            params.append(("speeds", speeds_key))
        if ratings_key:
            params.append(("ratings", ratings_key))
        url = "https://explorer.lichess.ovh/lichess?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            self._cache[cache_key] = data
            return data
        except Exception:
            return None

    @staticmethod
    def _weighted_avg_wr(moves: list, color: str) -> float:
        """Weighted average win rate for the position (weighted by games per move)."""
        total_games = 0
        weighted_sum = 0.0
        for m in moves:
            pw = m.get("white", 0)
            pd = m.get("draws", 0)
            pb = m.get("black", 0)
            pt = pw + pd + pb
            if pt == 0:
                continue
            wr = 100.0 * (pw if color == "white" else pb) / pt
            weighted_sum += wr * pt
            total_games += pt
        return weighted_sum / total_games if total_games > 0 else 50.0
