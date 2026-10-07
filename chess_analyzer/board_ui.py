"""The board widget: rendering and mouse/keyboard input on top of a ``GameState``.

Engine results arrive through the ``EngineWorker`` (marshalled by the ``UiDispatcher``) and
are stored in the game state's per-ply analysis; the app and its panels read that state and
talk to this widget only through its public methods and the three callbacks.
"""

from __future__ import annotations

import logging
import math
import tkinter as tk
from collections.abc import Callable, Sequence

import chess

from . import evaluation, theme
from .engine_worker import EngineWorker, EvalRequest, EvalResult, Kind
from .game_state import GameState
from .pieces import PieceImages, piece_key

log = logging.getLogger(__name__)

# ── Layout ───────────────────────────────────────────────────────────────────
SQUARE_SIZE = 80
MIN_SQUARE = 50
MAX_SQUARE = 130
OFFSET_X = 20  # left margin: rank labels
OFFSET_Y = 6
BOTTOM_MARGIN = 20  # file labels and the throbber
EVAL_BAR_W = 28
MINI_BOX_W = 32  # hover number box
MINI_BOX_H = 14
BAR_RANGE_CP = 1200  # ±range mapped onto the eval bar
CLICK_SLOP_PX = 4
ANIM_FRAMES = 8
ANIM_FRAME_MS = 30
THROBBER_MS = 40
PARTIAL_REDRAW_MS = 80

PROMOTION_PIECES = (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT)
PROMOTION_KEYS = {"q": chess.QUEEN, "r": chess.ROOK, "b": chess.BISHOP, "n": chess.KNIGHT}

# Canvas tags from bottom to top; _raise_layers() enforces this order after every draw.
LAYERS = (
    "board",
    "lastmove",
    "heatmap",
    "select",
    "arrow",
    "pv_arrow",
    "notable_arrow",
    "pieces",
    "eval",
    "promo",
    "drag",
    "anim",
    "throbber",
)

MoveScores = dict[chess.Square, list[tuple[chess.Move, float]]]


class BoardUI(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        worker: EngineWorker,
        state: GameState,
        pieces: PieceImages,
        depth: int = 15,
        on_move: Callable[[chess.Move], None] | None = None,
        on_navigate: Callable[[int], None] | None = None,
        on_evals_updated: Callable[[int, bool], None] | None = None,
    ) -> None:
        super().__init__(parent, bg=theme.BG)
        self._worker = worker
        self._state = state
        self._pieces = pieces
        self._depth = depth
        self._on_move = on_move
        self._on_navigate = on_navigate
        self._on_evals_updated = on_evals_updated

        self._sq = SQUARE_SIZE
        self._board_px = 8 * SQUARE_SIZE
        self._flipped = False
        self._show_arrow = False
        self._heatmap_on = True
        self._explorer_move_order: list[str] = []

        # Hover
        self._hovered_square: chess.Square | None = None
        self._hover_id: int | None = None
        # Selection (click-to-move)
        self._selected: set[chess.Square] = set()
        self._legal_from_selected: list[chess.Move] = []
        # Heatmap / analysis of the shown position
        self._heatmap_id: int | None = None
        self._heatmap_data: MoveScores = {}
        self._rank_map: dict[str, float] = {}
        self._best_move: chess.Move | None = None
        self._position_cp: int | None = None
        self._partial_after_id: str | None = None
        # Arrows
        self._pv_moves: list[chess.Move] = []
        self._notable: tuple[str, chess.Move] | None = None  # (fen it belongs to, move)
        self._notable_move: chess.Move | None = None  # drawn when the shown fen matches
        # Drag / animation / promotion
        self._drag_from: chess.Square | None = None
        self._drag_press = (0, 0)
        self._drag_pos = (0, 0)
        self._animating = False
        self._anim_after_id: str | None = None
        self._anim_complete: Callable[[], None] | None = None
        self._pending_promotion: tuple[chess.Square, chess.Square] | None = None
        self._promo_squares: dict[chess.Square, chess.PieceType] = {}
        # Throbber
        self._throbber_active = False
        self._throbber_angle = 0.0
        self._throbber_after_id: str | None = None

        self._piece_images: dict[str, object] = {}
        self._load_piece_images()

        total_w, total_h = self._canvas_size()
        self._eval_canvas = tk.Canvas(
            self,
            width=EVAL_BAR_W,
            height=total_h,
            bg=theme.EVAL_BAR_BG,
            highlightthickness=1,
            highlightbackground=theme.BORDER_LIGHT,
        )
        self._eval_canvas.pack(side="left", padx=(0, 4))
        self._canvas = tk.Canvas(
            self, width=total_w, height=total_h, bg=theme.BG, highlightthickness=0
        )
        self._canvas.pack(side="left")
        self._bar_items = self._create_eval_bar()

        self._canvas.bind("<Motion>", self._on_mouse_move)
        self._canvas.bind("<Leave>", self._on_mouse_leave)
        self._canvas.bind("<ButtonPress-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<Key>", self._on_key)

        self._load_analysis_for_current_ply()
        self._full_redraw()
        self._request_heatmap()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> GameState:
        return self._state

    @property
    def board(self) -> chess.Board:
        """A copy of the position currently shown."""
        return self._state.board

    @property
    def flipped(self) -> bool:
        return self._flipped

    @property
    def best_move(self) -> chess.Move | None:
        return self._best_move

    @property
    def position_cp(self) -> int | None:
        return self._position_cp

    @property
    def heatmap_data(self) -> MoveScores:
        return self._heatmap_data

    @property
    def rank_map(self) -> dict[str, float]:
        return self._rank_map

    @property
    def square_size(self) -> int:
        return self._sq

    def get_fen(self) -> str:
        return self.board.fen()

    def load_board(self, board: chess.Board) -> None:
        """Show a position with no move history."""
        self._settle_animation()
        self._cancel_promotion()
        self._state.load_board(board)
        self._show_position()

    def load_game(
        self,
        start_board: chess.Board,
        moves: Sequence[chess.Move],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Load a whole game and show its final position."""
        self._settle_animation()
        self._cancel_promotion()
        self._state.load_game(start_board, list(moves), headers)
        self._show_position()

    def goto(self, ply: int) -> None:
        self._settle_animation()
        self._cancel_promotion()
        if self._state.goto(ply) == self._state.current_ply and ply != self._state.current_ply:
            return
        self._show_position()

    def first(self) -> None:
        self._goto_if_changed(0)

    def prev(self) -> None:
        self._goto_if_changed(self._state.current_ply - 1)

    def next(self) -> None:
        self._goto_if_changed(self._state.current_ply + 1)

    def last(self) -> None:
        self._goto_if_changed(len(self._state.moves))

    def undo(self) -> None:
        self.prev()

    def play_move(self, move: chess.Move, animated: bool = True) -> bool:
        """Play a move from the shown position (as if the user had made it)."""
        if move not in self.board.legal_moves:
            return False
        self._settle_animation()
        self._cancel_promotion()
        self._execute_move(move, animated)
        return True

    def select_square(self, square: chess.Square | None) -> None:
        """Highlight a piece and its destinations (None clears the selection)."""
        self._selected = set()
        self._legal_from_selected = []
        if square is not None:
            legal = [m for m in self.board.legal_moves if m.from_square == square]
            if legal:
                self._selected = {square}
                self._legal_from_selected = legal
        self._draw_selection_overlays()

    def clear_selection(self) -> None:
        self.select_square(None)

    def set_flipped(self, flipped: bool) -> None:
        if flipped == self._flipped:
            return
        self._flipped = flipped
        self._full_redraw()

    def set_show_arrow(self, show: bool) -> None:
        self._show_arrow = show
        self._draw_best_move_arrow()
        self._raise_layers()

    def set_heatmap(self, enabled: bool) -> None:
        self._heatmap_on = enabled
        self._draw_heatmap()
        self._raise_layers()

    def set_depth(self, depth: int) -> None:
        if depth == self._depth:
            return
        self._depth = depth
        self._cancel_hover()
        self._canvas.delete("eval")
        self._request_heatmap()

    def set_explorer_move_order(self, ucis: Sequence[str]) -> None:
        """Most-played human moves first: the heatmap evaluates them first."""
        self._explorer_move_order = list(ucis)

    def resize(self, new_sq: int) -> None:
        new_sq = max(MIN_SQUARE, min(MAX_SQUARE, new_sq))
        if new_sq == self._sq:
            return
        self._sq = new_sq
        self._board_px = 8 * new_sq
        total_w, total_h = self._canvas_size()
        self._canvas.configure(width=total_w, height=total_h)
        self._eval_canvas.configure(height=total_h)
        self._load_piece_images()
        self._full_redraw()

    def show_pv(self, moves: Sequence[chess.Move]) -> None:
        """Draw colour-coded arrows for a line from the shown position."""
        board = self.board
        valid: list[chess.Move] = []
        for move in moves[: len(theme.PV_ARROWS)]:
            if move not in board.legal_moves:
                break
            valid.append(move)
            board.push(move)
        self._pv_moves = valid
        self._draw_pv_arrows()
        self._raise_layers()

    def clear_pv(self) -> None:
        self._pv_moves = []
        self._canvas.delete("pv_arrow")

    def show_notable(self, move: chess.Move | None) -> None:
        """Draw (or clear) the purple 'notable move' arrow for the shown position.

        The arrow belongs to this position: it hides when the user navigates away and comes
        back when they return, until it is cleared or replaced.
        """
        self._notable = (self.board.fen(), move) if move is not None else None
        self._notable_move = move
        self._draw_notable_arrow()
        self._raise_layers()

    # ── Position changes ──────────────────────────────────────────────────────

    def _goto_if_changed(self, ply: int) -> None:
        ply = max(0, min(ply, len(self._state.moves)))
        if ply == self._state.current_ply:
            return
        self.goto(ply)

    def _show_position(self) -> None:
        """The shown ply changed: reset overlays, redraw, ask the engine, tell the app."""
        self._cancel_hover()
        self._hovered_square = None
        self._selected = set()
        self._legal_from_selected = []
        self._pv_moves = []
        notable = self._notable
        self._notable_move = notable[1] if notable and notable[0] == self.board.fen() else None
        self._cancel_partial_refresh()
        self._load_analysis_for_current_ply()
        self._full_redraw()
        self._request_heatmap()
        if self._on_navigate is not None:
            self._on_navigate(self._state.current_ply)

    def _load_analysis_for_current_ply(self) -> None:
        analysis = self._state.analysis.get(self._state.current_ply)
        if analysis is None:
            self._heatmap_data, self._rank_map = {}, {}
            self._best_move, self._position_cp = None, None
            return
        self._heatmap_data = self._group_by_square(analysis.move_evals)
        self._rank_map = dict(analysis.rank_map)
        best = evaluation.best_uci(analysis.move_evals, self.board.turn)
        self._best_move = chess.Move.from_uci(best) if best else None
        self._position_cp = analysis.position_cp

    def _execute_move(self, move: chess.Move, animated: bool) -> None:
        piece = self.board.piece_at(move.from_square)
        if animated and piece is not None:
            self._animating = True
            self._canvas.delete("select")
            self._animate_move(move, piece, on_complete=lambda: self._finish_execute(move))
            return
        self._finish_execute(move)

    def _finish_execute(self, move: chess.Move) -> None:
        self._animating = False
        self._anim_after_id = None
        self._anim_complete = None
        self._canvas.delete("anim")
        self._state.push(move)
        self._show_position()
        if self._on_move is not None:
            self._on_move(move)

    # ── Engine requests ───────────────────────────────────────────────────────

    def _request_heatmap(self) -> None:
        ply = self._state.current_ply
        analysis = self._state.analysis.get(ply)
        if analysis is not None and analysis.complete and analysis.depth == self._depth:
            # Already analysed: no engine work, but listeners still get told it is done.
            self._heatmap_id = None
            self._stop_throbber()
            self.after(0, lambda: self._notify_evals(ply, True))
            return
        board = self.board
        legal = list(board.legal_moves)
        if not legal:
            # Mate or stalemate: nothing to evaluate, but the position still counts as analysed
            # so game analysis can move on.
            cp = self._state.terminal_eval(ply)
            self._state.set_analysis(
                ply, position_cp=cp, move_evals={}, rank_map={}, complete=True, depth=self._depth
            )
            self._heatmap_id = None
            self._position_cp = cp
            self._stop_throbber()
            self._update_eval_bar()
            self.after(0, lambda: self._notify_evals(ply, True))
            return
        if self._explorer_move_order:
            order = {uci: i for i, uci in enumerate(self._explorer_move_order)}
            legal.sort(key=lambda m: order.get(m.uci(), len(order)))
        self._start_throbber()
        self._heatmap_id = self._worker.submit(
            EvalRequest(
                kind=Kind.HEATMAP,
                board=board,
                moves=tuple(legal),
                depth=self._depth,
                on_done=self._receive_heatmap,
                on_progress=self._receive_partial_heatmap,
                tag=ply,
            )
        )

    def _request_hover_evals(self, square: chess.Square) -> None:
        board = self.board
        legal = [m for m in board.legal_moves if m.from_square == square]
        if not legal:
            return
        self._hover_id = self._worker.submit(
            EvalRequest(
                kind=Kind.HOVER,
                board=board,
                moves=tuple(legal),
                depth=self._depth,
                on_done=self._receive_hover,
            )
        )

    def _cancel_hover(self) -> None:
        if self._hover_id is not None:
            self._worker.cancel(self._hover_id)
            self._hover_id = None

    def _result_is_current(self, result: EvalResult, request_id: int | None) -> bool:
        return result.request_id == request_id and result.epd == self.board.epd()

    def _receive_heatmap(self, result: EvalResult) -> None:
        if not self._result_is_current(result, self._heatmap_id):
            return
        self._heatmap_id = None
        self._cancel_partial_refresh()
        self._stop_throbber()
        ply = self._state.current_ply
        self._ingest(result, complete=result.complete)
        self._draw_heatmap()
        self._update_eval_bar()
        self._draw_best_move_arrow()
        self._draw_selection_overlays()
        self._raise_layers()
        self._notify_evals(ply, result.complete)

    def _receive_partial_heatmap(self, result: EvalResult) -> None:
        if not self._result_is_current(result, self._heatmap_id):
            return
        self._ingest(result, complete=False)
        if self._partial_after_id is None:
            self._partial_after_id = self.after(PARTIAL_REDRAW_MS, self._partial_refresh)

    def _partial_refresh(self) -> None:
        self._partial_after_id = None
        self._draw_heatmap()
        self._update_eval_bar()
        self._draw_best_move_arrow()
        self._draw_selection_overlays()
        self._raise_layers()
        self._notify_evals(self._state.current_ply, False)

    def _cancel_partial_refresh(self) -> None:
        if self._partial_after_id is not None:
            self.after_cancel(self._partial_after_id)
            self._partial_after_id = None

    def _ingest(self, result: EvalResult, complete: bool) -> None:
        """Store an evaluation result for the shown ply and refresh the derived state."""
        board = self.board
        move_evals = dict(result.move_evals)
        rank_map = evaluation.rank_moves(move_evals, board.turn)
        best = evaluation.best_uci(move_evals, board.turn)
        position_cp = evaluation.cp_white(move_evals[best]) if best else None
        self._state.set_analysis(
            self._state.current_ply,
            position_cp=position_cp,
            move_evals=move_evals,
            rank_map=rank_map,
            complete=complete,
            depth=self._depth,
        )
        self._heatmap_data = self._group_by_square(move_evals)
        self._rank_map = rank_map
        self._best_move = chess.Move.from_uci(best) if best else None
        self._position_cp = position_cp

    def _group_by_square(self, move_evals: dict) -> MoveScores:
        by_square: MoveScores = {}
        for uci, move_eval in move_evals.items():
            move = chess.Move.from_uci(uci)
            pawns = evaluation.cp_white(move_eval) / 100.0
            by_square.setdefault(move.from_square, []).append((move, pawns))
        best_first = self.board.turn == chess.WHITE
        for moves in by_square.values():
            moves.sort(key=lambda item: item[1], reverse=best_first)
        return by_square

    def _notify_evals(self, ply: int, complete: bool) -> None:
        if self._on_evals_updated is not None:
            self._on_evals_updated(ply, complete)

    def _receive_hover(self, result: EvalResult) -> None:
        if not self._result_is_current(result, self._hover_id):
            return
        self._hover_id = None
        self._draw_hover_evals(result)

    # ── Mouse / keyboard ──────────────────────────────────────────────────────

    def _on_mouse_move(self, event: tk.Event) -> None:
        if self._animating or self._pending_promotion is not None:
            return
        square = self._pixel_to_square(event.x, event.y)
        if square == self._hovered_square:
            return
        self._hovered_square = square
        self._cancel_hover()
        self._canvas.delete("eval")
        if square is None:
            return
        board = self.board
        piece = board.piece_at(square)
        if piece is None or not any(m.from_square == square for m in board.legal_moves):
            return
        self._request_hover_evals(square)

    def _on_mouse_leave(self, _event: tk.Event) -> None:
        self._hovered_square = None
        self._cancel_hover()
        self._canvas.delete("eval")

    def _on_press(self, event: tk.Event) -> None:
        self._canvas.focus_set()
        if self._animating:
            return
        square = self._pixel_to_square(event.x, event.y)
        if self._pending_promotion is not None:
            if square in self._promo_squares:
                self._choose_promotion(self._promo_squares[square])
            else:
                self._cancel_promotion()
            return
        self._drag_press = (event.x, event.y)
        self._drag_pos = (event.x, event.y)
        if square is None:
            return
        board = self.board
        piece = board.piece_at(square)
        if piece is None or not any(m.from_square == square for m in board.legal_moves):
            return
        self._drag_from = square
        self._draw_piece_at(piece, event.x, event.y, "drag")
        self._raise_layers()

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_from is None:
            return
        dx, dy = event.x - self._drag_pos[0], event.y - self._drag_pos[1]
        self._canvas.move("drag", dx, dy)
        self._drag_pos = (event.x, event.y)
        self._canvas.delete("drag_target")
        target = self._pixel_to_square(event.x, event.y)
        if (
            target is not None
            and target != self._drag_from
            and any(m.to_square == target for m in self._legal_moves_from(self._drag_from))
        ):
            x, y = self._sq_to_pixel(target)
            self._canvas.create_rectangle(
                x + 2,
                y + 2,
                x + self._sq - 2,
                y + self._sq - 2,
                outline=theme.GOLD,
                width=3,
                tags="drag_target",
            )
        self._canvas.tag_raise("drag")

    def _on_release(self, event: tk.Event) -> None:
        self._canvas.delete("drag_target")
        drag_from, self._drag_from = self._drag_from, None
        self._canvas.delete("drag")
        if self._animating or self._pending_promotion is not None:
            return
        target = self._pixel_to_square(event.x, event.y)
        moved = abs(event.x - self._drag_press[0]) > CLICK_SLOP_PX or (
            abs(event.y - self._drag_press[1]) > CLICK_SLOP_PX
        )
        if drag_from is not None and moved and target is not None:
            self._try_move(drag_from, target, animated=False)
        elif drag_from is not None:
            self._handle_click(drag_from)
        elif target is not None:
            self._handle_click(target)

    def _on_key(self, event: tk.Event) -> None:
        if self._pending_promotion is None:
            return
        if event.keysym == "Escape":
            self._cancel_promotion()
        elif event.char.lower() in PROMOTION_KEYS:
            self._choose_promotion(PROMOTION_KEYS[event.char.lower()])

    def _legal_moves_from(self, square: chess.Square) -> list[chess.Move]:
        return [m for m in self.board.legal_moves if m.from_square == square]

    def _try_move(self, from_sq: chess.Square, to_sq: chess.Square, animated: bool) -> None:
        legal = [m for m in self._legal_moves_from(from_sq) if m.to_square == to_sq]
        if not legal:
            return
        if any(m.promotion for m in legal):
            self._begin_promotion(from_sq, to_sq)
            return
        self._execute_move(legal[0], animated)

    def _handle_click(self, square: chess.Square) -> None:
        board = self.board
        clicked = board.piece_at(square)
        dest_moves = [m for m in self._legal_from_selected if m.to_square == square]
        if dest_moves:
            from_sq = dest_moves[0].from_square
            if len({m.from_square for m in dest_moves}) > 1:
                # Two selected pieces can reach this square: keep the selection, ask for one.
                return
            self._try_move(from_sq, square, animated=True)
        elif square in self._selected:
            self._selected.discard(square)
            self._legal_from_selected = [
                m for m in board.legal_moves if m.from_square in self._selected
            ]
            self._draw_selection_overlays()
        elif (
            clicked is not None
            and self._selected
            and clicked.color == board.turn
            and self._legal_moves_from(square)
        ):
            self._selected.add(square)
            self._legal_from_selected = [
                m for m in board.legal_moves if m.from_square in self._selected
            ]
            self._draw_selection_overlays()
        elif clicked is not None and self._legal_moves_from(square):
            self._selected = {square}
            self._legal_from_selected = self._legal_moves_from(square)
            self._draw_selection_overlays()
        else:
            self.clear_selection()

    # ── Promotion chooser (drawn on the board) ────────────────────────────────

    def _begin_promotion(self, from_sq: chess.Square, to_sq: chess.Square) -> None:
        self._pending_promotion = (from_sq, to_sq)
        step = -8 if chess.square_rank(to_sq) == 7 else 8
        squares = [to_sq + i * step for i in range(len(PROMOTION_PIECES))]
        self._promo_squares = dict(zip(squares, PROMOTION_PIECES, strict=True))
        self._selected = set()
        self._legal_from_selected = []
        self._canvas.delete("select")
        self._draw_promotion_chooser()
        self._canvas.focus_set()

    def _draw_promotion_chooser(self) -> None:
        self._canvas.delete("promo")
        if self._pending_promotion is None:
            return
        colour = self.board.turn
        for square, piece_type in self._promo_squares.items():
            x, y = self._sq_to_pixel(square)
            self._canvas.create_rectangle(
                x + 1,
                y + 1,
                x + self._sq - 1,
                y + self._sq - 1,
                fill=theme.TABLE_BG,
                outline=theme.GOLD,
                width=2,
                tags="promo",
            )
            self._draw_piece_at(
                chess.Piece(piece_type, colour), x + self._sq // 2, y + self._sq // 2, "promo"
            )
        self._raise_layers()

    def _choose_promotion(self, piece_type: chess.PieceType) -> None:
        if self._pending_promotion is None:
            return
        from_sq, to_sq = self._pending_promotion
        self._cancel_promotion()
        move = chess.Move(from_sq, to_sq, promotion=piece_type)
        if move in self.board.legal_moves:
            self._execute_move(move, animated=True)

    def _cancel_promotion(self) -> None:
        self._pending_promotion = None
        self._promo_squares = {}
        self._canvas.delete("promo")

    # ── Animation ─────────────────────────────────────────────────────────────

    def _animate_move(
        self, move: chess.Move, piece: chess.Piece, on_complete: Callable[[], None]
    ) -> None:
        """Slide the piece from source to destination, then run ``on_complete``."""
        x1, y1 = self._sq_to_pixel(move.from_square)
        x2, y2 = self._sq_to_pixel(move.to_square)
        half = self._sq // 2
        cx1, cy1, cx2, cy2 = x1 + half, y1 + half, x2 + half, y2 + half
        self._draw_piece_at(piece, cx1, cy1, "anim")
        self._raise_layers()
        self._anim_complete = on_complete

        def step(frame: int) -> None:
            self._anim_after_id = None
            if frame > ANIM_FRAMES:
                self._canvas.delete("anim")
                complete, self._anim_complete = self._anim_complete, None
                if complete is not None:
                    complete()
                return
            t = frame / ANIM_FRAMES
            eased = 1 - (1 - t) ** 3
            self._canvas.coords(
                "anim", int(cx1 + (cx2 - cx1) * eased), int(cy1 + (cy2 - cy1) * eased)
            )
            self._anim_after_id = self.after(ANIM_FRAME_MS, lambda: step(frame + 1))

        step(1)

    def _settle_animation(self) -> None:
        """Finish a move animation immediately (so the move is never lost or applied twice)."""
        if not self._animating:
            return
        if self._anim_after_id is not None:
            self.after_cancel(self._anim_after_id)
            self._anim_after_id = None
        complete, self._anim_complete = self._anim_complete, None
        self._canvas.delete("anim")
        self._animating = False
        if complete is not None:
            complete()

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _canvas_size(self) -> tuple[int, int]:
        return OFFSET_X + self._board_px + OFFSET_X, OFFSET_Y + self._board_px + BOTTOM_MARGIN

    def _load_piece_images(self) -> None:
        self._piece_images = dict(self._pieces.at(self._sq - 8))

    def _full_redraw(self) -> None:
        self._canvas.delete("all")
        self._draw_board()
        self._draw_labels()
        self._draw_last_move_highlight()
        self._draw_heatmap()
        self._draw_pieces()
        self._draw_selection_overlays()
        self._draw_best_move_arrow()
        self._draw_pv_arrows()
        self._draw_notable_arrow()
        self._draw_promotion_chooser()
        if self._throbber_active:
            self._draw_throbber_frame()
        self._update_eval_bar()
        self._raise_layers()

    def _raise_layers(self) -> None:
        for tag in LAYERS:
            self._canvas.tag_raise(tag)

    def _draw_board(self) -> None:
        self._canvas.create_rectangle(
            OFFSET_X - 2,
            OFFSET_Y - 2,
            OFFSET_X + self._board_px + 2,
            OFFSET_Y + self._board_px + 2,
            outline=theme.BORDER,
            width=2,
            fill="",
            tags="board",
        )
        for square in chess.SQUARES:
            x, y = self._sq_to_pixel(square)
            light = (chess.square_file(square) + chess.square_rank(square)) % 2 == 1
            self._canvas.create_rectangle(
                x,
                y,
                x + self._sq,
                y + self._sq,
                fill=theme.LIGHT_SQ if light else theme.DARK_SQ,
                outline="",
                tags="board",
            )

    def _draw_labels(self) -> None:
        font = theme.ui(max(8, int(self._sq * 0.14)), "bold")
        files = "hgfedcba" if self._flipped else "abcdefgh"
        ranks = range(1, 9) if self._flipped else range(8, 0, -1)
        for row, rank in enumerate(ranks):
            self._canvas.create_text(
                OFFSET_X // 2,
                OFFSET_Y + row * self._sq + self._sq // 2,
                text=str(rank),
                font=font,
                fill=theme.BOARD_LABEL,
                tags="board",
            )
        for col, file in enumerate(files):
            self._canvas.create_text(
                OFFSET_X + col * self._sq + self._sq // 2,
                OFFSET_Y + self._board_px + 10,
                text=file,
                font=font,
                fill=theme.BOARD_LABEL,
                tags="board",
            )

    def _draw_last_move_highlight(self) -> None:
        self._canvas.delete("lastmove")
        last = self._state.last_move()
        if last is None:
            return
        x, y = self._sq_to_pixel(last.from_square)
        self._canvas.create_rectangle(
            x,
            y,
            x + self._sq,
            y + self._sq,
            fill=theme.LAST_MOVE,
            outline="",
            stipple="gray25",
            tags="lastmove",
        )
        x, y = self._sq_to_pixel(last.to_square)
        self._canvas.create_rectangle(
            x,
            y,
            x + self._sq,
            y + self._sq,
            fill=theme.LAST_MOVE,
            outline="",
            stipple="gray50",
            tags="lastmove",
        )
        self._canvas.create_rectangle(
            x + 2,
            y + 2,
            x + self._sq - 2,
            y + self._sq - 2,
            fill="",
            outline=theme.LAST_MOVE_TO_BORDER,
            width=3,
            tags="lastmove",
        )

    def _draw_pieces(self) -> None:
        self._canvas.delete("pieces")
        for square, piece in self.board.piece_map().items():
            x, y = self._sq_to_pixel(square)
            self._draw_piece_at(piece, x + self._sq // 2, y + self._sq // 2, "pieces")

    def _draw_piece_at(self, piece: chess.Piece, cx: int, cy: int, tag: str) -> None:
        image = self._piece_images.get(piece_key(piece))
        if image is not None:
            self._canvas.create_image(cx, cy, image=image, tags=tag)
            return
        glyph = piece.unicode_symbol()
        font = theme.symbol(max(12, int(self._sq * 0.5)))
        white = piece.color == chess.WHITE
        self._canvas.create_text(
            cx + 1,
            cy + 2,
            text=glyph,
            font=font,
            fill=theme.GLYPH_SHADOW_WHITE if white else theme.GLYPH_SHADOW_BLACK,
            tags=tag,
        )
        self._canvas.create_text(
            cx,
            cy + 1,
            text=glyph,
            font=font,
            fill=theme.GLYPH_WHITE if white else theme.GLYPH_BLACK,
            tags=tag,
        )

    def _draw_heatmap(self) -> None:
        self._canvas.delete("heatmap")
        if not self._heatmap_on:
            return
        for square, move_scores in self._heatmap_data.items():
            self._draw_heatmap_for_square(square, move_scores)

    def _draw_heatmap_for_square(
        self, square: chess.Square, move_scores: Sequence[tuple[chess.Move, float]]
    ) -> None:
        n = len(move_scores)
        if n == 0:
            return
        x, y = self._sq_to_pixel(square)
        for i, (move, _pawns) in enumerate(move_scores):
            colour = theme.rank_color(self._rank_map.get(move.uci(), 0.5))
            y0 = y + round(i * self._sq / n)
            y1 = y + round((i + 1) * self._sq / n)
            self._canvas.create_rectangle(
                x, y0, x + self._sq, y1, fill=colour, outline="", tags="heatmap"
            )

    def _draw_selection_overlays(self) -> None:
        self._canvas.delete("select")
        if not self._selected:
            return
        for square in self._selected:
            x, y = self._sq_to_pixel(square)
            self._canvas.create_rectangle(
                x + 2,
                y + 2,
                x + self._sq - 2,
                y + self._sq - 2,
                outline=theme.SEL_BORDER,
                width=3,
                tags="select",
            )
        seen: set[chess.Square] = set()
        radius = max(5, self._sq // 8)
        for move in self._legal_from_selected:
            if move.to_square in seen:
                continue
            seen.add(move.to_square)
            x, y = self._sq_to_pixel(move.to_square)
            cx, cy = x + self._sq // 2, y + self._sq // 2
            t = self._rank_map.get(move.uci())
            colour = theme.rank_color(t) if t is not None else theme.SEL_DOT
            self._canvas.create_oval(
                cx - radius,
                cy - radius,
                cx + radius,
                cy + radius,
                fill=colour,
                outline="",
                tags="select",
            )
        self._raise_layers()

    def _draw_arrow(self, move: chess.Move, colour: str, width: int, tag: str) -> None:
        x1, y1 = self._sq_to_pixel(move.from_square)
        x2, y2 = self._sq_to_pixel(move.to_square)
        half = self._sq // 2
        head = max(10, self._sq // 4)
        self._canvas.create_line(
            x1 + half,
            y1 + half,
            x2 + half,
            y2 + half,
            arrow=tk.LAST,
            arrowshape=(head, head + 4, head // 2),
            width=width,
            fill=colour,
            tags=tag,
        )

    def _draw_best_move_arrow(self) -> None:
        self._canvas.delete("arrow")
        if self._show_arrow and self._best_move is not None:
            self._draw_arrow(self._best_move, theme.GOLD, max(4, self._sq // 11), "arrow")

    def _draw_pv_arrows(self) -> None:
        self._canvas.delete("pv_arrow")
        widths = (7, 5, 4, 3, 2)
        for i, move in enumerate(self._pv_moves):
            self._draw_arrow(move, theme.PV_ARROWS[i], widths[i], "pv_arrow")

    def _draw_notable_arrow(self) -> None:
        self._canvas.delete("notable_arrow")
        if self._notable_move is not None:
            self._draw_arrow(
                self._notable_move, theme.NOTABLE_ARROW, max(4, self._sq // 11), "notable_arrow"
            )

    def _draw_hover_evals(self, result: EvalResult) -> None:
        self._canvas.delete("eval")
        board = self.board
        best = evaluation.best_uci(result.move_evals, board.turn)
        for uci, move_eval in result.move_evals.items():
            move = chess.Move.from_uci(uci)
            t = self._rank_map.get(uci)
            if t is not None:
                border = theme.rank_color(t)
            else:
                border = theme.GOLD if uci == best else theme.HIGHLIGHT_OTHER
            self._draw_eval_on_square(move.to_square, evaluation.cp_white(move_eval), border)
        self._raise_layers()

    def _draw_eval_on_square(self, square: chess.Square, cp_white: int, border: str) -> None:
        x, y = self._sq_to_pixel(square)
        self._canvas.create_rectangle(
            x + self._sq - MINI_BOX_W,
            y,
            x + self._sq,
            y + MINI_BOX_H,
            fill=theme.BORDER,
            outline="",
            tags="eval",
        )
        self._canvas.create_text(
            x + self._sq - 2,
            y + 2,
            anchor="ne",
            text=evaluation.format_cp(cp_white),
            font=theme.ui(8, "bold"),
            fill=theme.GOLD if border == theme.GOLD else theme.WHITE,
            tags="eval",
        )
        self._canvas.create_rectangle(
            x + 1, y + 1, x + self._sq - 1, y + self._sq - 1, outline=border, width=2, tags="eval"
        )

    # ── Eval bar (created once, updated in place) ─────────────────────────────

    def _create_eval_bar(self) -> dict[str, int]:
        c = self._eval_canvas
        return {
            "black": c.create_rectangle(0, 0, 0, 0, fill=theme.EVAL_BAR_BLACK, outline=""),
            "white": c.create_rectangle(0, 0, 0, 0, fill=theme.EVAL_BAR_WHITE, outline=""),
            "split": c.create_line(0, 0, 0, 0, fill=theme.EVAL_BAR_SPLIT),
            "text": c.create_text(0, 0, text="", font=theme.ui(8, "bold")),
        }

    def _update_eval_bar(self) -> None:
        c = self._eval_canvas
        top, bottom = OFFSET_Y, OFFSET_Y + self._board_px
        cp = self._position_cp
        ratio = 0.5 if cp is None else max(0.05, min(0.95, cp / BAR_RANGE_CP + 0.5))
        white_px = int((bottom - top) * ratio)
        if self._flipped:  # White at the top
            white_box = (2, top, EVAL_BAR_W - 2, top + white_px)
            black_box = (2, top + white_px, EVAL_BAR_W - 2, bottom)
            split = top + white_px
        else:
            black_box = (2, top, EVAL_BAR_W - 2, bottom - white_px)
            white_box = (2, bottom - white_px, EVAL_BAR_W - 2, bottom)
            split = bottom - white_px
        c.coords(self._bar_items["black"], *black_box)
        c.coords(self._bar_items["white"], *white_box)
        c.coords(self._bar_items["split"], 1, split, EVAL_BAR_W - 1, split)
        if cp is None:
            c.itemconfigure(self._bar_items["text"], text="")
            return
        in_white = ratio > 0.5
        at_top = in_white == self._flipped
        c.coords(self._bar_items["text"], EVAL_BAR_W // 2, top + 10 if at_top else bottom - 10)
        c.itemconfigure(
            self._bar_items["text"],
            text=evaluation.format_bar_text(cp),
            fill=theme.BORDER if in_white else theme.TEXT_BRIGHT,
            font=theme.ui(max(6, int(self._sq * 0.09)), "bold"),
        )

    # ── Throbber ──────────────────────────────────────────────────────────────

    def _start_throbber(self) -> None:
        if self._throbber_active:
            return
        self._throbber_active = True
        self._throbber_angle = 0.0
        self._animate_throbber()

    def _stop_throbber(self) -> None:
        self._throbber_active = False
        if self._throbber_after_id is not None:
            self.after_cancel(self._throbber_after_id)
            self._throbber_after_id = None
        self._canvas.delete("throbber")

    def _animate_throbber(self) -> None:
        self._throbber_after_id = None
        if not self._throbber_active:
            return
        self._draw_throbber_frame()
        self._throbber_angle = (self._throbber_angle + 10) % 360
        self._throbber_after_id = self.after(THROBBER_MS, self._animate_throbber)

    def _draw_throbber_frame(self) -> None:
        self._canvas.delete("throbber")
        n = 12
        cx = OFFSET_X + self._board_px // 2
        cy = OFFSET_Y + self._board_px + 10
        for i in range(n):
            angle = math.radians(self._throbber_angle + i * 360 / n)
            t = i / (n - 1)
            r = 3 * (0.35 + 0.65 * t)
            x, y = cx + 7 * math.cos(angle), cy + 7 * math.sin(angle)
            self._canvas.create_oval(
                x - r, y - r, x + r, y + r, fill=theme.rank_color(t), outline="", tags="throbber"
            )

    # ── Coordinates ───────────────────────────────────────────────────────────

    def _sq_to_pixel(self, square: chess.Square) -> tuple[int, int]:
        file, rank = chess.square_file(square), chess.square_rank(square)
        col = 7 - file if self._flipped else file
        row = rank if self._flipped else 7 - rank
        return OFFSET_X + col * self._sq, OFFSET_Y + row * self._sq

    def _pixel_to_square(self, px: int, py: int) -> chess.Square | None:
        col = (px - OFFSET_X) // self._sq
        row = (py - OFFSET_Y) // self._sq
        if not (0 <= col < 8 and 0 <= row < 8):
            return None
        file = 7 - col if self._flipped else col
        rank = row if self._flipped else 7 - row
        return chess.square(file, rank)
