"""The vertical evaluation bar beside the board and the evaluation graph below it."""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import evaluation, theme
from .board_widget import BAR_RANGE_CP, BoardWidget

BAR_WIDTH = 26
GRAPH_HEIGHT = 72
GRAPH_RANGE_CP = 700


class EvalBar(QWidget):
    """Who is winning, aligned with the board next to it."""

    def __init__(self, board: BoardWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._board = board
        self._cp: int | None = None
        self.setFixedWidth(BAR_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        board.geometryChanged.connect(self.update)

    def set_cp(self, cp: int | None) -> None:
        if cp != self._cp:
            self._cp = cp
            self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        board_rect = self._board.board_rect()
        top, height = board_rect.top(), board_rect.height()
        area = QRect(0, top, self.width(), height)
        painter.fillRect(area, QColor(theme.EVAL_BAR_BG))
        cp = self._cp
        ratio = 0.5 if cp is None else max(0.05, min(0.95, cp / BAR_RANGE_CP + 0.5))
        white_px = int(height * ratio)
        inner = area.adjusted(2, 0, -2, 0)
        if self._board.flipped:  # White at the top
            white_rect = QRect(inner.left(), top, inner.width(), white_px)
            black_rect = QRect(inner.left(), top + white_px, inner.width(), height - white_px)
            split = top + white_px
        else:
            black_rect = QRect(inner.left(), top, inner.width(), height - white_px)
            white_rect = QRect(inner.left(), top + height - white_px, inner.width(), white_px)
            split = top + height - white_px
        painter.fillRect(black_rect, QColor(theme.EVAL_BAR_BLACK))
        painter.fillRect(white_rect, QColor(theme.EVAL_BAR_WHITE))
        painter.setPen(QPen(QColor(theme.EVAL_BAR_SPLIT), 1))
        painter.drawLine(1, split, self.width() - 2, split)
        if cp is None:
            return
        font = QFont(self.font())
        font.setPointSizeF(7.5)
        font.setBold(True)
        painter.setFont(font)
        in_white = ratio > 0.5
        at_top = in_white == self._board.flipped
        text_rect = (
            QRect(0, top + 2, self.width(), 16)
            if at_top
            else QRect(0, top + height - 18, self.width(), 16)
        )
        painter.setPen(QColor(theme.BORDER if in_white else theme.TEXT_BRIGHT))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, evaluation.format_bar_text(cp))


class EvalGraph(QWidget):
    """White's advantage over the course of the game; click to jump to a ply."""

    plyClicked = Signal(int)

    def __init__(self, board: BoardWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._board = board
        self.setFixedHeight(GRAPH_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def refresh(self) -> None:
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(theme.CANVAS_BG))
        state = self._board.state
        evals = state.position_evals()
        total = len(state.moves) + 1
        cy = h / 2
        painter.setPen(QPen(QColor(theme.GRAPH_AXIS), 1))
        painter.drawLine(0, int(cy), w, int(cy))
        if total < 2 or not evals:
            pen = QPen(QColor(theme.GRAPH_PLACEHOLDER), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(0, int(cy), w, int(cy))
            return
        prev: tuple[float, float] | None = None
        white_fill = QColor(theme.GRAPH_WHITE_FILL)
        white_fill.setAlpha(140)
        black_fill = QColor(theme.GRAPH_BLACK_FILL)
        black_fill.setAlpha(170)
        line_pen = QPen(QColor(theme.GRAPH_LINE), 2)
        for ply in range(total):
            if ply not in evals:
                continue
            x = ply / (total - 1) * (w - 1)
            cp = max(-GRAPH_RANGE_CP, min(GRAPH_RANGE_CP, evals[ply]))
            y = cy - cp / GRAPH_RANGE_CP * (cy - 4)
            if prev is not None:
                px, py = prev
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(white_fill)
                painter.drawPolygon(
                    [_pt(px, cy), _pt(px, min(py, cy)), _pt(x, min(y, cy)), _pt(x, cy)]
                )
                painter.setBrush(black_fill)
                painter.drawPolygon(
                    [_pt(px, cy), _pt(px, max(py, cy)), _pt(x, max(y, cy)), _pt(x, cy)]
                )
                painter.setPen(line_pen)
                painter.drawLine(_pt(px, py), _pt(x, y))
            prev = (x, y)
        cx = state.current_ply / (total - 1) * (w - 1)
        painter.setPen(QPen(QColor(theme.MARKER_LINE), 2))
        painter.drawLine(_pt(cx, 0), _pt(cx, h))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        total = len(self._board.state.moves) + 1
        if total < 2:
            return
        ply = round(event.position().x() / max(1, self.width() - 1) * (total - 1))
        self.plyClicked.emit(max(0, min(total - 1, ply)))


def _pt(x: float, y: float):
    from PySide6.QtCore import QPointF

    return QPointF(x, y)
