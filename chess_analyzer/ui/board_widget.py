"""The board: QPainter rendering and mouse/keyboard input on top of a ``GameState``.

Engine results arrive through the ``QtDispatcher`` on the GUI thread and are stored in the
game state's per-ply analysis. Other widgets observe the board through its signals and read
its ``state``; they never touch its internals.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence

import chess
from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
    QResizeEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import evaluation, theme
from ..engine_worker import EngineWorker, EvalRequest, EvalResult, Kind
from ..game_state import GameState
from ..pieces import piece_key
from .piece_pixmaps import PiecePixmaps

log = logging.getLogger(__name__)

MIN_SQUARE = 24
CLICK_SLOP_PX = 4
ANIM_MS = 220
PARTIAL_REDRAW_MS = 80
BAR_RANGE_CP = 1200

PROMOTION_PIECES = (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT)
PROMOTION_KEYS = {
    Qt.Key.Key_Q: chess.QUEEN,
    Qt.Key.Key_R: chess.ROOK,
    Qt.Key.Key_B: chess.BISHOP,
    Qt.Key.Key_N: chess.KNIGHT,
}

MoveScores = dict[chess.Square, list[tuple[chess.Move, float]]]


def _rgba(colour: str, alpha: int = 255) -> QColor:
    c = QColor(colour)
    c.setAlpha(alpha)
    return c


class BoardWidget(QWidget):
    navigated = Signal(int)  # the shown ply changed (navigation, new move or new game)
    moved = Signal(object)  # a move was played on the board (chess.Move)
    evalsUpdated = Signal(int, bool)  # engine results for a ply arrived (ply, complete)
    thinking = Signal(bool)  # the heatmap for the shown position is (not) in flight
    geometryChanged = Signal()  # the board square/rect changed (eval bar follows it)

    def __init__(
        self,
        *,
        state: GameState,
        pixmaps: PiecePixmaps,
        depth: int = 15,
        worker: EngineWorker | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._pixmaps = pixmaps
        self._depth = depth
        self._worker = worker

        self._sq = 64
        self._board_rect = QRect(0, 0, 8 * self._sq, 8 * self._sq)
        self._flipped = False
        self._show_arrow = False
        self._heatmap_on = True
        self._explorer_move_order: list[str] = []
        # Puzzle mode: no engine requests, no scores on the board; moves the user makes go
        # through the interceptor first (False = rejected, the move is not played).
        self._puzzle_mode = False
        self._interceptor: Callable[[chess.Move], bool] | None = None
        self._flash: tuple[set[chess.Square], QColor] | None = None
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._clear_flash)

        # Hover
        self._hovered: chess.Square | None = None
        self._hover_id: int | None = None
        self._hover_evals: dict[str, object] = {}
        # Selection
        self._selected: set[chess.Square] = set()
        self._legal_from_selected: list[chess.Move] = []
        # Analysis of the shown position
        self._heatmap_id: int | None = None
        self._heatmap_data: MoveScores = {}
        self._rank_map: dict[str, float] = {}
        self._best_move: chess.Move | None = None
        self._position_cp: int | None = None
        self._partial_timer = QTimer(self)
        self._partial_timer.setSingleShot(True)
        self._partial_timer.setInterval(PARTIAL_REDRAW_MS)
        self._partial_timer.timeout.connect(self._partial_refresh)
        # Arrows
        self._pv_moves: list[chess.Move] = []
        self._notable: tuple[str, chess.Move] | None = None
        self._notable_move: chess.Move | None = None
        # Drag / animation / promotion
        self._press_pos: QPoint | None = None
        self._drag_from: chess.Square | None = None
        self._dragging = False
        self._drag_pos = QPoint()
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(lambda _v: self.update())
        self._anim.finished.connect(self._on_animation_finished)
        self._anim_move: chess.Move | None = None
        self._anim_piece: chess.Piece | None = None
        self._pending_promotion: tuple[chess.Square, chess.Square] | None = None
        self._promo_squares: dict[chess.Square, chess.PieceType] = {}

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(8 * MIN_SQUARE, 8 * MIN_SQUARE)
        self._load_analysis_for_current_ply()

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

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def puzzle_mode(self) -> bool:
        return self._puzzle_mode

    def board_rect(self) -> QRect:
        return QRect(self._board_rect)

    def set_worker(self, worker: EngineWorker) -> None:
        """Attach the engine once it is ready; evaluates the shown position right away."""
        self._worker = worker
        self._request_heatmap()

    def get_fen(self) -> str:
        return self.board.fen()

    def load_board(self, board: chess.Board) -> None:
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
        self._settle_animation()
        self._cancel_promotion()
        self._state.load_game(start_board, list(moves), headers)
        self._show_position()

    def goto(self, ply: int) -> None:
        ply = max(0, min(ply, len(self._state.moves)))
        if ply == self._state.current_ply and not self._anim_move:
            return
        self._settle_animation()
        self._cancel_promotion()
        self._state.goto(ply)
        self._show_position()

    def first(self) -> None:
        self.goto(0)

    def prev(self) -> None:
        self.goto(self._state.current_ply - 1)

    def next(self) -> None:
        self.goto(self._state.current_ply + 1)

    def last(self) -> None:
        self.goto(len(self._state.moves))

    def undo(self) -> None:
        self.prev()

    def play_move(self, move: chess.Move, animated: bool = True) -> bool:
        if move not in self.board.legal_moves:
            return False
        self._settle_animation()
        self._cancel_promotion()
        self._execute_move(move, animated)
        return True

    def select_square(self, square: chess.Square | None) -> None:
        self._selected = set()
        self._legal_from_selected = []
        if square is not None:
            legal = self._legal_moves_from(square)
            if legal:
                self._selected = {square}
                self._legal_from_selected = legal
        self.update()

    def clear_selection(self) -> None:
        self.select_square(None)

    def set_flipped(self, flipped: bool) -> None:
        if flipped != self._flipped:
            self._flipped = flipped
            self.update()
            self.geometryChanged.emit()

    def set_show_arrow(self, show: bool) -> None:
        self._show_arrow = show
        self.update()

    def set_heatmap(self, enabled: bool) -> None:
        self._heatmap_on = enabled
        self.update()

    def set_depth(self, depth: int) -> None:
        if depth == self._depth:
            return
        self._depth = depth
        self._cancel_hover()
        self._request_heatmap()

    def set_puzzle_mode(self, enabled: bool) -> None:
        """Hide everything that could give a puzzle away and stop asking the engine.

        The heatmap and arrow settings are left as they are; they come back on exit.
        """
        if enabled == self._puzzle_mode:
            return
        self._puzzle_mode = enabled
        self._cancel_hover()
        self._hover_evals = {}
        if enabled:
            if self._heatmap_id is not None and self._worker is not None:
                self._worker.cancel(self._heatmap_id)
            self._heatmap_id = None
            self.thinking.emit(False)
        else:
            self._request_heatmap()
        self.update()

    def set_move_interceptor(self, fn: Callable[[chess.Move], bool] | None) -> None:
        """Moves the user makes on the board are offered to ``fn`` first; False rejects one."""
        self._interceptor = fn

    def swap_state(self, state: GameState) -> GameState:
        """Show another game state (its analysis untouched) and return the previous one."""
        self._settle_animation()
        self._cancel_promotion()
        self._cancel_hover()
        if self._heatmap_id is not None and self._worker is not None:
            self._worker.cancel(self._heatmap_id)
        self._heatmap_id = None
        old, self._state = self._state, state
        self._show_position()
        return old

    def flash_squares(
        self, squares: Sequence[chess.Square], colour: str = theme.FLASH_BAD, ms: int = 600
    ) -> None:
        self._flash = (set(squares), _rgba(colour, 170))
        self._flash_timer.start(ms)
        self.update()

    def _clear_flash(self) -> None:
        self._flash = None
        self.update()

    def set_explorer_move_order(self, ucis: Sequence[str]) -> None:
        self._explorer_move_order = list(ucis)

    def show_pv(self, moves: Sequence[chess.Move]) -> None:
        board = self.board
        valid: list[chess.Move] = []
        for move in moves[: len(theme.PV_ARROWS)]:
            if move not in board.legal_moves:
                break
            valid.append(move)
            board.push(move)
        self._pv_moves = valid
        self.update()

    def clear_pv(self) -> None:
        if self._pv_moves:
            self._pv_moves = []
            self.update()

    def show_notable(self, move: chess.Move | None) -> None:
        """The purple arrow belongs to the shown position: hidden elsewhere, back on return."""
        self._notable = (self.board.fen(), move) if move is not None else None
        self._notable_move = move
        self.update()

    # ── Position changes ──────────────────────────────────────────────────────

    def _show_position(self) -> None:
        self._cancel_hover()
        self._hovered = None
        self._hover_evals = {}
        self._selected = set()
        self._legal_from_selected = []
        self._pv_moves = []
        notable = self._notable
        self._notable_move = notable[1] if notable and notable[0] == self.board.fen() else None
        self._partial_timer.stop()
        self._load_analysis_for_current_ply()
        self.update()
        self._request_heatmap()
        self.navigated.emit(self._state.current_ply)

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
            self._anim_move = move
            self._anim_piece = piece
            self._selected = set()
            self._legal_from_selected = []
            self._anim.start()
            return
        self._finish_execute(move)

    def _on_animation_finished(self) -> None:
        move, self._anim_move, self._anim_piece = self._anim_move, None, None
        if move is not None:
            self._finish_execute(move)

    def _settle_animation(self) -> None:
        """Finish a running move animation immediately so the move is applied exactly once."""
        if self._anim_move is None:
            return
        self._anim.stop()  # does not emit finished()
        move, self._anim_move, self._anim_piece = self._anim_move, None, None
        self._finish_execute(move)

    def _finish_execute(self, move: chess.Move) -> None:
        self._state.push(move)
        self._show_position()
        self.moved.emit(move)

    # ── Engine ────────────────────────────────────────────────────────────────

    def _request_heatmap(self) -> None:
        if self._puzzle_mode:
            return
        ply = self._state.current_ply
        analysis = self._state.analysis.get(ply)
        if analysis is not None and analysis.complete and analysis.depth == self._depth:
            self._heatmap_id = None
            self.thinking.emit(False)
            QTimer.singleShot(0, lambda: self.evalsUpdated.emit(ply, True))
            return
        board = self.board
        legal = list(board.legal_moves)
        if not legal:
            cp = self._state.terminal_eval(ply)
            self._state.set_analysis(
                ply, position_cp=cp, move_evals={}, rank_map={}, complete=True, depth=self._depth
            )
            self._heatmap_id = None
            self._position_cp = cp
            self.thinking.emit(False)
            self.update()
            QTimer.singleShot(0, lambda: self.evalsUpdated.emit(ply, True))
            return
        if self._worker is None:
            return
        if self._explorer_move_order:
            order = {uci: i for i, uci in enumerate(self._explorer_move_order)}
            legal.sort(key=lambda m: order.get(m.uci(), len(order)))
        self.thinking.emit(True)
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
        if self._worker is None or self._puzzle_mode:
            return
        legal = self._legal_moves_from(square)
        if not legal:
            return
        self._hover_id = self._worker.submit(
            EvalRequest(
                kind=Kind.HOVER,
                board=self.board,
                moves=tuple(legal),
                depth=self._depth,
                on_done=self._receive_hover,
            )
        )

    def _cancel_hover(self) -> None:
        if self._hover_id is not None and self._worker is not None:
            self._worker.cancel(self._hover_id)
        self._hover_id = None

    def _is_current(self, result: EvalResult, request_id: int | None) -> bool:
        return result.request_id == request_id and result.epd == self.board.epd()

    def _receive_heatmap(self, result: EvalResult) -> None:
        if not self._is_current(result, self._heatmap_id):
            return
        self._heatmap_id = None
        self._partial_timer.stop()
        self._ingest(result, complete=result.complete)
        self.thinking.emit(False)
        self.update()
        self.evalsUpdated.emit(self._state.current_ply, result.complete)

    def _receive_partial_heatmap(self, result: EvalResult) -> None:
        if not self._is_current(result, self._heatmap_id):
            return
        self._ingest(result, complete=False)
        if not self._partial_timer.isActive():
            self._partial_timer.start()

    def _partial_refresh(self) -> None:
        self.update()
        self.evalsUpdated.emit(self._state.current_ply, False)

    def _ingest(self, result: EvalResult, complete: bool) -> None:
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
            by_square.setdefault(move.from_square, []).append(
                (move, evaluation.cp_white(move_eval) / 100.0)
            )
        best_first = self.board.turn == chess.WHITE
        for moves in by_square.values():
            moves.sort(key=lambda item: item[1], reverse=best_first)
        return by_square

    def _receive_hover(self, result: EvalResult) -> None:
        if not self._is_current(result, self._hover_id):
            return
        self._hover_id = None
        self._hover_evals = dict(result.move_evals)
        self.update()

    # ── Geometry ──────────────────────────────────────────────────────────────

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        size = max(MIN_SQUARE, min(self.width(), self.height()) // 8)
        self._sq = size
        self._board_rect = QRect(
            (self.width() - 8 * size) // 2, (self.height() - 8 * size) // 2, 8 * size, 8 * size
        )
        self.geometryChanged.emit()

    def _square_rect(self, square: chess.Square) -> QRect:
        file, rank = chess.square_file(square), chess.square_rank(square)
        col = 7 - file if self._flipped else file
        row = rank if self._flipped else 7 - rank
        return QRect(
            self._board_rect.left() + col * self._sq,
            self._board_rect.top() + row * self._sq,
            self._sq,
            self._sq,
        )

    def _square_centre(self, square: chess.Square) -> QPointF:
        return QPointF(self._square_rect(square).center()) + QPointF(0.5, 0.5)

    def _square_at(self, pos: QPoint) -> chess.Square | None:
        if not self._board_rect.contains(pos):
            return None
        col = (pos.x() - self._board_rect.left()) // self._sq
        row = (pos.y() - self._board_rect.top()) // self._sq
        if not (0 <= col < 8 and 0 <= row < 8):
            return None
        return self._square_for_cell(col, row)

    def _square_for_cell(self, col: int, row: int) -> chess.Square:
        """The square drawn in grid cell (col, row), honouring the flip."""
        file = 7 - col if self._flipped else col
        rank = row if self._flipped else 7 - row
        return chess.square(file, rank)

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        board = self.board
        self._paint_squares(painter)
        self._paint_last_move(painter)
        if self._heatmap_on and not self._puzzle_mode:
            self._paint_heatmap(painter)
        self._paint_flash(painter)
        self._paint_selection(painter)
        self._paint_pieces(painter, board)
        if self._heatmap_on and not self._puzzle_mode:
            self._paint_best_star(painter)
        self._paint_arrows(painter)
        self._paint_hover_evals(painter)
        self._paint_coordinates(painter)
        self._paint_promotion(painter, board)
        self._paint_moving_piece(painter)
        painter.end()

    def _paint_squares(self, painter: QPainter) -> None:
        light, dark = QColor(theme.LIGHT_SQ), QColor(theme.DARK_SQ)
        painter.setPen(Qt.PenStyle.NoPen)
        for square in chess.SQUARES:
            is_light = (chess.square_file(square) + chess.square_rank(square)) % 2 == 1
            painter.fillRect(self._square_rect(square), light if is_light else dark)

    def _paint_last_move(self, painter: QPainter) -> None:
        last = self._state.last_move()
        if last is None:
            return
        painter.fillRect(self._square_rect(last.from_square), _rgba(theme.LAST_MOVE, 110))
        painter.fillRect(self._square_rect(last.to_square), _rgba(theme.LAST_MOVE, 160))

    def _paint_flash(self, painter: QPainter) -> None:
        if self._flash is None:
            return
        squares, colour = self._flash
        for square in squares:
            painter.fillRect(self._square_rect(square), colour)

    def _paint_heatmap(self, painter: QPainter) -> None:
        for square, move_scores in self._heatmap_data.items():
            n = len(move_scores)
            if n == 0:
                continue
            rect = self._square_rect(square)
            # Best move in the top band, down to the worst at the bottom.
            ordered = sorted(move_scores, key=lambda ms: -self._rank_map.get(ms[0].uci(), 0.5))
            for i, (move, _pawns) in enumerate(ordered):
                colour = QColor(theme.rank_color(self._rank_map.get(move.uci(), 0.5)))
                y0 = rect.top() + round(i * rect.height() / n)
                y1 = rect.top() + round((i + 1) * rect.height() / n)
                painter.fillRect(QRect(rect.left(), y0, rect.width(), y1 - y0), colour)

    def _paint_selection(self, painter: QPainter) -> None:
        if not self._selected:
            return
        pen = QPen(QColor(theme.SEL_BORDER), 3)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for square in self._selected:
            painter.drawRect(self._square_rect(square).adjusted(2, 2, -2, -2))
        painter.setPen(Qt.PenStyle.NoPen)
        radius = max(5, self._sq / 8)
        seen: set[chess.Square] = set()
        for move in self._legal_from_selected:
            if move.to_square in seen:
                continue
            seen.add(move.to_square)
            t = None if self._puzzle_mode else self._rank_map.get(move.uci())
            colour = QColor(theme.rank_color(t)) if t is not None else QColor(theme.SEL_DOT)
            colour.setAlpha(230)
            painter.setBrush(colour)
            painter.drawEllipse(self._square_centre(move.to_square), radius, radius)

    def _paint_pieces(self, painter: QPainter, board: chess.Board) -> None:
        pixmaps = self._pixmaps.at(self._sq - max(4, self._sq // 10))
        skip: set[chess.Square] = set()
        if self._dragging and self._drag_from is not None:
            skip.add(self._drag_from)
        if self._anim_move is not None:
            skip.add(self._anim_move.from_square)
        for square, piece in board.piece_map().items():
            if square in skip:
                continue
            self._draw_piece(painter, pixmaps, piece, self._square_centre(square))

    def _draw_piece(
        self, painter: QPainter, pixmaps: dict, piece: chess.Piece, centre: QPointF
    ) -> None:
        pixmap = pixmaps.get(piece_key(piece))
        if pixmap is None:  # missing image: a glyph
            painter.setPen(QColor(theme.GLYPH_WHITE if piece.color else theme.GLYPH_BLACK))
            painter.setFont(QFont("Segoe UI Symbol", max(10, int(self._sq * 0.5))))
            painter.drawText(
                QRectF(centre.x() - self._sq / 2, centre.y() - self._sq / 2, self._sq, self._sq),
                Qt.AlignmentFlag.AlignCenter,
                piece.unicode_symbol(),
            )
            return
        painter.drawPixmap(
            QPointF(centre.x() - pixmap.width() / 2, centre.y() - pixmap.height() / 2), pixmap
        )

    def _paint_best_star(self, painter: QPainter) -> None:
        """A small gold star in the top-right corner of the square whose piece has the best move."""
        if self._best_move is None or not self._heatmap_data:
            return
        rect = self._square_rect(self._best_move.from_square)
        outer = max(5.0, self._sq * 0.13)
        inner = outer * 0.45
        cx = rect.right() - outer - self._sq * 0.04
        cy = rect.top() + outer + self._sq * 0.04
        points = []
        for i in range(10):
            radius = outer if i % 2 == 0 else inner
            angle = -math.pi / 2 + i * math.pi / 5
            points.append(QPointF(cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        painter.setPen(QPen(QColor("#5A4500"), max(1.0, self._sq / 60)))
        painter.setBrush(QColor(theme.GOLD))
        painter.drawPolygon(QPolygonF(points))
        painter.setPen(Qt.PenStyle.NoPen)

    def _paint_arrows(self, painter: QPainter) -> None:
        width = max(4, self._sq / 11)
        if self._show_arrow and self._best_move is not None and not self._puzzle_mode:
            self._draw_arrow(painter, self._best_move, _rgba(theme.GOLD, 215), width)
        widths = (7, 5, 4, 3, 2)
        for i, move in enumerate(self._pv_moves):
            self._draw_arrow(painter, move, _rgba(theme.PV_ARROWS[i], 215), widths[i])
        if self._notable_move is not None:
            self._draw_arrow(painter, self._notable_move, _rgba(theme.NOTABLE_ARROW, 225), width)

    def _draw_arrow(
        self, painter: QPainter, move: chess.Move, colour: QColor, width: float
    ) -> None:
        start = self._square_centre(move.from_square)
        end = self._square_centre(move.to_square)
        dx, dy = end.x() - start.x(), end.y() - start.y()
        length = math.hypot(dx, dy)
        if length < 1:
            return
        ux, uy = dx / length, dy / length
        head = max(10.0, self._sq * 0.32)
        shaft_end = QPointF(end.x() - ux * head * 0.8, end.y() - uy * head * 0.8)
        painter.setPen(QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(start, shaft_end)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour))
        half = head * 0.55
        tip = end
        base = QPointF(end.x() - ux * head, end.y() - uy * head)
        painter.drawPolygon(
            QPolygonF(
                [
                    tip,
                    QPointF(base.x() - uy * half, base.y() + ux * half),
                    QPointF(base.x() + uy * half, base.y() - ux * half),
                ]
            )
        )

    def _paint_hover_evals(self, painter: QPainter) -> None:
        if not self._hover_evals:
            return
        board = self.board
        best = evaluation.best_uci(self._hover_evals, board.turn)
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, self._sq * 0.13))
        font.setBold(True)
        painter.setFont(font)
        box_h = max(14, int(self._sq * 0.22))
        for uci, move_eval in self._hover_evals.items():
            move = chess.Move.from_uci(uci)
            t = self._rank_map.get(uci)
            if t is not None:
                border = QColor(theme.rank_color(t))
            else:
                border = QColor(theme.GOLD if uci == best else theme.HIGHLIGHT_OTHER)
            rect = self._square_rect(move.to_square)
            painter.setPen(QPen(border, 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(2, 2, -2, -2))
            text = evaluation.format_cp(evaluation.cp_white(move_eval))
            box = QRect(
                rect.right() - int(self._sq * 0.5), rect.top() + 2, int(self._sq * 0.5) - 2, box_h
            )
            painter.fillRect(box, _rgba(theme.BORDER, 215))
            painter.setPen(
                QColor(theme.GOLD if border.name().upper() == theme.GOLD else theme.WHITE)
            )
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_coordinates(self, painter: QPainter) -> None:
        font = QFont(self.font())
        font.setPointSizeF(max(6.5, self._sq * 0.14))
        font.setBold(True)
        painter.setFont(font)
        for col in range(8):  # bottom row: file letters in the bottom-right corner
            square = self._square_for_cell(col, 7)
            painter.setPen(self._coordinate_colour(square))
            painter.drawText(
                self._square_rect(square).adjusted(0, 0, -3, -1),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                chess.FILE_NAMES[chess.square_file(square)],
            )
        for row in range(8):  # left column: rank numbers in the top-left corner
            square = self._square_for_cell(0, row)
            painter.setPen(self._coordinate_colour(square))
            painter.drawText(
                self._square_rect(square).adjusted(3, 1, 0, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                str(chess.square_rank(square) + 1),
            )

    @staticmethod
    def _coordinate_colour(square: chess.Square) -> QColor:
        """Contrast with the square: dark text on light squares and vice versa."""
        is_light = (chess.square_file(square) + chess.square_rank(square)) % 2 == 1
        return _rgba(theme.DARK_SQ if is_light else theme.LIGHT_SQ, 235)

    def _paint_promotion(self, painter: QPainter, board: chess.Board) -> None:
        if self._pending_promotion is None:
            return
        painter.fillRect(self._board_rect, _rgba("#000000", 110))
        pixmaps = self._pixmaps.at(self._sq - max(4, self._sq // 10))
        for square, piece_type in self._promo_squares.items():
            rect = self._square_rect(square)
            painter.setPen(QPen(QColor(theme.GOLD), 2))
            painter.setBrush(QColor(theme.TABLE_BG))
            painter.drawRoundedRect(QRectF(rect).adjusted(1.5, 1.5, -1.5, -1.5), 6, 6)
            self._draw_piece(
                painter, pixmaps, chess.Piece(piece_type, board.turn), self._square_centre(square)
            )

    def _paint_moving_piece(self, painter: QPainter) -> None:
        pixmaps = self._pixmaps.at(self._sq - max(4, self._sq // 10))
        if self._dragging and self._drag_from is not None:
            piece = self.board.piece_at(self._drag_from)
            if piece is not None:
                self._draw_piece(painter, pixmaps, piece, QPointF(self._drag_pos))
        if self._anim_move is not None and self._anim_piece is not None:
            t = float(self._anim.currentValue() or 0.0)
            start = self._square_centre(self._anim_move.from_square)
            end = self._square_centre(self._anim_move.to_square)
            centre = QPointF(
                start.x() + (end.x() - start.x()) * t, start.y() + (end.y() - start.y()) * t
            )
            self._draw_piece(painter, pixmaps, self._anim_piece, centre)

    # ── Mouse and keyboard ────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        square = self._square_at(pos)
        if self._anim_move is not None:
            return
        if self._pending_promotion is not None:
            if square in self._promo_squares:
                self._choose_promotion(self._promo_squares[square])
            else:
                self._cancel_promotion()
            return
        self._press_pos = pos
        self._drag_pos = pos
        self._dragging = False
        self._drag_from = None
        if square is None:
            return
        piece = self.board.piece_at(square)
        if piece is not None and self._legal_moves_from(square):
            self._drag_from = square

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_from is not None:
            if (
                not self._dragging
                and self._press_pos is not None
                and (pos - self._press_pos).manhattanLength() > CLICK_SLOP_PX
            ):
                self._dragging = True
                self._cancel_hover()
                self._hover_evals = {}
            if self._dragging:
                self._drag_pos = pos
                self.update()
            return
        if self._anim_move is not None or self._pending_promotion is not None:
            return
        square = self._square_at(pos)
        if square == self._hovered:
            return
        self._hovered = square
        self._cancel_hover()
        if self._hover_evals:
            self._hover_evals = {}
            self.update()
        if square is None:
            return
        piece = self.board.piece_at(square)
        if piece is not None and self._legal_moves_from(square):
            self._request_hover_evals(square)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        drag_from, self._drag_from = self._drag_from, None
        was_dragging, self._dragging = self._dragging, False
        if self._anim_move is not None or self._pending_promotion is not None:
            self.update()
            return
        square = self._square_at(pos)
        if drag_from is not None and was_dragging:
            if square is not None:
                self._try_move(drag_from, square, animated=False)
            self.update()
        elif drag_from is not None:
            self._handle_click(drag_from)
        elif square is not None:
            self._handle_click(square)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._hovered = None
        self._cancel_hover()
        if self._hover_evals:
            self._hover_evals = {}
            self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._pending_promotion is not None:
            if event.key() == Qt.Key.Key_Escape:
                self._cancel_promotion()
                return
            piece_type = PROMOTION_KEYS.get(event.key())
            if piece_type is not None:
                self._choose_promotion(piece_type)
                return
        super().keyPressEvent(event)

    def _legal_moves_from(self, square: chess.Square) -> list[chess.Move]:
        return [m for m in self.board.legal_moves if m.from_square == square]

    def _try_move(self, from_sq: chess.Square, to_sq: chess.Square, animated: bool) -> None:
        legal = [m for m in self._legal_moves_from(from_sq) if m.to_square == to_sq]
        if not legal:
            return
        if any(m.promotion for m in legal):
            self._begin_promotion(from_sq, to_sq)
            return
        self._user_move(legal[0], animated)

    def _user_move(self, move: chess.Move, animated: bool) -> None:
        """A move the user made with the mouse: offered to the interceptor first."""
        if self._interceptor is not None and not self._interceptor(move):
            self.clear_selection()
            self.flash_squares([move.from_square, move.to_square])
            return
        self._execute_move(move, animated)

    def _handle_click(self, square: chess.Square) -> None:
        board = self.board
        clicked = board.piece_at(square)
        dest_moves = [m for m in self._legal_from_selected if m.to_square == square]
        if dest_moves:
            if len({m.from_square for m in dest_moves}) > 1:
                return  # two selected pieces can reach it; the user must pick one
            self._try_move(dest_moves[0].from_square, square, animated=True)
        elif square in self._selected:
            self._selected.discard(square)
            self._legal_from_selected = [
                m for m in board.legal_moves if m.from_square in self._selected
            ]
            self.update()
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
            self.update()
        elif clicked is not None and self._legal_moves_from(square):
            self._selected = {square}
            self._legal_from_selected = self._legal_moves_from(square)
            self.update()
        else:
            self.clear_selection()

    # ── Promotion chooser ─────────────────────────────────────────────────────

    def _begin_promotion(self, from_sq: chess.Square, to_sq: chess.Square) -> None:
        self._pending_promotion = (from_sq, to_sq)
        step = -8 if chess.square_rank(to_sq) == 7 else 8
        squares = [to_sq + i * step for i in range(len(PROMOTION_PIECES))]
        self._promo_squares = dict(zip(squares, PROMOTION_PIECES, strict=True))
        self._selected = set()
        self._legal_from_selected = []
        self._cancel_hover()
        self._hover_evals = {}
        self.setFocus()
        self.update()

    def _choose_promotion(self, piece_type: chess.PieceType) -> None:
        if self._pending_promotion is None:
            return
        from_sq, to_sq = self._pending_promotion
        self._cancel_promotion()
        move = chess.Move(from_sq, to_sq, promotion=piece_type)
        if move in self.board.legal_moves:
            self._user_move(move, animated=True)

    def _cancel_promotion(self) -> None:
        if self._pending_promotion is None:
            return
        self._pending_promotion = None
        self._promo_squares = {}
        self.update()
