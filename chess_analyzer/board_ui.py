"""
board_ui.py
tkinter Canvas chess board with hover-triggered engine evaluation overlays,
click-to-move piece interaction, heatmap, eval bar, move history navigation,
board flip, and best-move arrow.
"""

import math
import tkinter as tk
from pathlib import Path
from typing import Callable, Dict, List, Optional

import chess
import chess.pgn

from engine_worker import EvalRequest, EvalResult, EngineWorker

try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# ── Layout constants ──────────────────────────────────────────────────────────
SQUARE_SIZE = 80          # px per square → 640 px board
OFFSET_X = 20             # left margin — rank labels live here
OFFSET_Y = 6              # top border gap
BOARD_PX = SQUARE_SIZE * 8
EVAL_BAR_W = 28           # width of the vertical position eval bar

# ── Eval overlay constants ────────────────────────────────────────────────────
EVAL_BAR_H = 14           # px tall for the mini eval bar
EVAL_MAX = 5.0            # clamp score to ±5 pawns

# ── Colors ────────────────────────────────────────────────────────────────────
LIGHT_SQ = "#F0D9B5"
DARK_SQ  = "#B58863"
HIGHLIGHT_BEST  = "#FFD700"
HIGHLIGHT_OTHER = "#88BBFF"
BAR_BG    = "#1A1A1A"
BAR_WHITE = "#E0E0E0"
BAR_CENTER = "#888888"
TEXT_BEST  = "#FFD700"
TEXT_OTHER = "#FFFFFF"

SEL_BORDER      = "#F6F669"   # soft yellow (Lichess-style)
SEL_DOT         = "#F6F669"
LAST_MOVE_COLOR = "#CDD16A"   # yellow-green (Lichess last-move)
LAST_MOVE_TO_BORDER = "#F0E050"  # bright gold border on landing square

# ── Piece glyphs (fallback when images unavailable) ───────────────────────────
PIECE_GLYPHS: Dict[chess.PieceType, Dict[chess.Color, str]] = {
    chess.KING:   {chess.WHITE: "♔", chess.BLACK: "♚"},
    chess.QUEEN:  {chess.WHITE: "♕", chess.BLACK: "♛"},
    chess.ROOK:   {chess.WHITE: "♖", chess.BLACK: "♜"},
    chess.BISHOP: {chess.WHITE: "♗", chess.BLACK: "♝"},
    chess.KNIGHT: {chess.WHITE: "♘", chess.BLACK: "♞"},
    chess.PAWN:   {chess.WHITE: "♙", chess.BLACK: "♟"},
}

# Piece key helpers
_PIECE_LETTERS = {
    chess.KING: "K", chess.QUEEN: "Q", chess.ROOK: "R",
    chess.BISHOP: "B", chess.KNIGHT: "N", chess.PAWN: "P",
}

def _piece_key(piece: chess.Piece) -> str:
    return ("w" if piece.color == chess.WHITE else "b") + _PIECE_LETTERS[piece.piece_type]

PIECE_FONT = ("Segoe UI Symbol", 40)
EVAL_FONT  = ("Segoe UI", 8, "bold")

PROMO_PIECES = [
    (chess.QUEEN,  "Queen"),
    (chess.ROOK,   "Rook"),
    (chess.BISHOP, "Bishop"),
    (chess.KNIGHT, "Knight"),
]


def _score_to_color(t: float) -> str:
    """Map t∈[0,1] → red (#D22828) at 0, amber at 0.5, green (#28C828) at 1."""
    t = max(0.0, min(1.0, t))
    r = int(210 * (1 - t) + 40 * t)
    g = int(40  * (1 - t) + 200 * t)
    b = 40
    return f"#{r:02X}{g:02X}{b:02X}"


class BoardUI:
    def __init__(
        self,
        parent: tk.Widget,
        engine_worker: EngineWorker,
        initial_board: chess.Board,
        display_mode: str = "both",
        history_callback: Optional[Callable] = None,
        pieces_dir: Optional[Path] = None,
    ) -> None:
        self._worker = engine_worker
        self._board = initial_board.copy()
        self._display_mode = display_mode
        self._history_callback = history_callback  # called after any navigation

        # Dynamic sizing (supports resize)
        self._sq: int = SQUARE_SIZE        # pixels per square
        self._board_px: int = SQUARE_SIZE * 8
        self._pieces_dir: Optional[Path] = pieces_dir  # store for reload on resize

        # Game history state
        self._game_start_board: chess.Board = initial_board.copy()
        self._game_moves: List[chess.Move] = []
        self._current_ply: int = 0

        # Flip state
        self._flipped: bool = False

        # Arrow state
        self._show_arrow: bool = True
        self._best_move: Optional[chess.Move] = None

        # Position eval state
        self._position_eval: Optional[float] = None  # centipawns, white perspective

        # Hover state
        self._hovered_square: Optional[chess.Square] = None
        self._current_request_id: int = 0
        self._last_moves: List[chess.Move] = []

        # Click / selection state
        self._selected_squares: set = set()
        self._legal_from_selected: List[chess.Move] = []

        # Heatmap state
        self._heatmap_mode: bool = False
        self._heatmap_request_id: int = 0
        self._heatmap_data: dict = {}
        self._rank_map: dict = {}
        self._ply_rank_maps: dict = {}    # ply → rank_map for moves FROM that position
        self._ply_move_scores: dict = {}  # ply → {uci: chess.engine.Score} raw scores

        # Eval graph data (ply → centipawns white POV, filled as positions are analyzed)
        self._ply_position_evals: dict = {}

        # Explorer move order (most-played human moves first, for engine sort)
        self._explorer_move_order: list = []

        self.on_heatmap_done: Optional[Callable] = None  # set by main.py for game analysis

        # Throbber state
        self._throbber_active: bool = False
        self._throbber_angle: float = 0.0

        # Drag state
        self._drag_from: Optional[chess.Square] = None
        self._drag_press: tuple = (0, 0)   # original press coords (for click/drag detection)
        self._drag_pos: tuple = (0, 0)     # last motion coords (for incremental canvas.move)

        # Move animation state
        self._animating: bool = False

        # Piece images (loaded from pieces_dir if Pillow available)
        self._piece_images: Dict[str, object] = {}  # key "wK" etc. → ImageTk.PhotoImage
        if pieces_dir is not None and _PIL_AVAILABLE:
            self._load_piece_images(pieces_dir)

        total_w = OFFSET_X + self._board_px + OFFSET_X
        total_h = OFFSET_Y + self._board_px + 20

        # Container frame holds eval bar + board canvas side by side
        self._container = tk.Frame(parent, bg="#2B2B2B")
        self._container.pack(padx=4, pady=4)

        # Vertical position eval bar
        self._eval_canvas = tk.Canvas(
            self._container,
            width=EVAL_BAR_W,
            height=total_h,
            bg="#1A1A1A",
            highlightthickness=1,
            highlightbackground="#555555",
        )
        self._eval_canvas.pack(side="left", padx=(0, 4))

        self._canvas = tk.Canvas(
            self._container,
            width=total_w,
            height=total_h,
            bg="#2B2B2B",
            highlightthickness=0,
        )
        self._canvas.pack(side="left")

        self._draw_board()
        self._draw_labels()
        self._draw_pieces()
        self._draw_eval_bar()

        self._canvas.bind("<Motion>", self._on_mouse_move)
        self._canvas.bind("<Leave>", self._on_mouse_leave)
        self._canvas.bind("<ButtonPress-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)

    # ── Piece image loading ───────────────────────────────────────────────────

    def _load_piece_images(self, pieces_dir: Path) -> None:
        size = self._sq - 8  # slight margin inside the square
        for key in ("wK", "wQ", "wR", "wB", "wN", "wP",
                    "bK", "bQ", "bR", "bB", "bN", "bP"):
            path = pieces_dir / f"{key}.png"
            if not path.exists():
                continue
            try:
                img = Image.open(path).convert("RGBA").resize(
                    (size, size), Image.LANCZOS
                )
                self._piece_images[key] = ImageTk.PhotoImage(img)
            except Exception:
                pass  # fall back to glyph for this piece

    # ── Public API ────────────────────────────────────────────────────────────

    def load_board(self, board: chess.Board) -> None:
        """Load a new position, resetting all game history."""
        self._game_start_board = board.copy()
        self._game_moves = []
        self._current_ply = 0
        self._best_move = None
        self._position_eval = None
        self._selected_squares = set()
        self._legal_from_selected = []
        self._hovered_square = None
        self._current_request_id += 1
        self._heatmap_data = {}
        self._rank_map = {}
        self._ply_rank_maps = {}
        self._ply_move_scores = {}
        self._ply_position_evals = {}
        self._board = board.copy()
        self._canvas.delete("eval")
        self._canvas.delete("select")
        self._canvas.delete("lastmove")
        self._canvas.delete("heatmap")
        self._canvas.delete("arrow")
        self._draw_pieces()
        self._draw_eval_bar()
        self._request_heatmap()
        if self._history_callback:
            self._history_callback()

    def load_game(self, start_board: chess.Board, moves: List[chess.Move]) -> None:
        """Load a full game (for PGN); navigate to last position."""
        self._game_start_board = start_board.copy()
        self._game_moves = list(moves)
        self._ply_position_evals = {}
        self._ply_move_scores = {}
        self._goto_ply(len(moves))

    def navigate_first(self) -> None:
        self._goto_ply(0)

    def navigate_prev(self) -> None:
        self._goto_ply(self._current_ply - 1)

    def navigate_next(self) -> None:
        self._goto_ply(self._current_ply + 1)

    def navigate_last(self) -> None:
        self._goto_ply(len(self._game_moves))

    def navigate_to_ply(self, ply: int) -> None:
        self._goto_ply(ply)

    def set_display_mode(self, mode: str) -> None:
        self._display_mode = mode
        if self._hovered_square is not None and self._last_moves:
            self._canvas.delete("eval")
            self._request_evals_for_square(self._hovered_square)

    def undo_move(self) -> None:
        self.navigate_prev()

    def set_heatmap_mode(self, enabled: bool) -> None:
        self._heatmap_mode = enabled
        if enabled:
            if self._heatmap_data:
                self._canvas.delete("heatmap")
                for sq, move_scores in self._heatmap_data.items():
                    self._draw_heatmap_for_square(sq, move_scores, self._rank_map)
                self._canvas.tag_raise("eval")
                self._canvas.tag_raise("select")
                self._canvas.tag_raise("arrow")
                self._canvas.tag_raise("pieces")
            else:
                self._request_heatmap()
        else:
            self._canvas.delete("heatmap")

    def set_flipped(self, flipped: bool) -> None:
        self._flipped = flipped
        self._canvas.delete("board")
        self._canvas.delete("labels")
        self._canvas.delete("lastmove")
        self._canvas.delete("heatmap")
        self._canvas.delete("eval")
        self._canvas.delete("select")
        self._canvas.delete("arrow")
        self._draw_board()
        self._draw_labels()
        self._draw_last_move_highlight()
        self._draw_pieces()
        if self._heatmap_mode and self._heatmap_data:
            for sq, move_scores in self._heatmap_data.items():
                self._draw_heatmap_for_square(sq, move_scores, self._rank_map)
        self._draw_best_move_arrow()
        self._canvas.tag_raise("eval")
        self._canvas.tag_raise("select")
        self._canvas.tag_raise("arrow")
        self._canvas.tag_raise("pieces")

    def set_show_arrow(self, show: bool) -> None:
        self._show_arrow = show
        self._draw_best_move_arrow()

    def resize(self, new_sq: int) -> None:
        """Resize the board to new_sq pixels per square and redraw everything."""
        new_sq = max(50, min(130, new_sq))
        if new_sq == self._sq:
            return
        self._sq = new_sq
        self._board_px = self._sq * 8
        total_w = OFFSET_X + self._board_px + OFFSET_X
        total_h = OFFSET_Y + self._board_px + 20
        self._canvas.configure(width=total_w, height=total_h)
        self._eval_canvas.configure(height=total_h)
        if self._pieces_dir is not None and _PIL_AVAILABLE:
            self._load_piece_images(self._pieces_dir)
        self._full_redraw()

    def _full_redraw(self) -> None:
        """Redraw the entire board from scratch."""
        self._canvas.delete("board")
        self._canvas.delete("labels")
        self._canvas.delete("lastmove")
        self._canvas.delete("heatmap")
        self._canvas.delete("eval")
        self._canvas.delete("select")
        self._canvas.delete("arrow")
        self._canvas.delete("pv_arrow")
        self._draw_board()
        self._draw_labels()
        self._draw_last_move_highlight()
        self._draw_pieces()
        if self._heatmap_mode and self._heatmap_data:
            for sq, move_scores in self._heatmap_data.items():
                self._draw_heatmap_for_square(sq, move_scores, self._rank_map)
        self._draw_best_move_arrow()
        self._draw_eval_bar()
        self._draw_selection_overlays()
        self._canvas.tag_raise("eval")
        self._canvas.tag_raise("select")
        self._canvas.tag_raise("arrow")
        self._canvas.tag_raise("pieces")

    def set_explorer_move_order(self, uci_list: list) -> None:
        """Set the move order from the opening explorer (most-played first)."""
        self._explorer_move_order = uci_list

    def get_fen(self) -> str:
        return self._board.fen()

    def get_pgn(self) -> str:
        game = chess.pgn.Game()
        game.setup(self._game_start_board)
        node = game
        board = self._game_start_board.copy()
        for move in self._game_moves:
            node = node.add_variation(move)
            board.push(move)
        exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
        return game.accept(exporter)

    def receive_eval_result(self, result: EvalResult) -> None:
        if result.request_id != self._current_request_id:
            return

        best_uci: Optional[str] = None
        best_val = float("-inf")
        for uci, score in result.move_evals.items():
            v = score.white().score(mate_score=50000)
            if v is not None and v > best_val:
                best_val = v
                best_uci = uci

        for uci, score in result.move_evals.items():
            move = chess.Move.from_uci(uci)
            if uci in self._rank_map:
                t = self._rank_map[uci]
                border_color = "#FFD700" if t >= 1.0 else _score_to_color(t)
            else:
                border_color = HIGHLIGHT_BEST if uci == best_uci else HIGHLIGHT_OTHER
            self._draw_eval_on_square(
                square=move.to_square,
                score=score,
                border_color=border_color,
            )

        self._canvas.tag_raise("select")
        self._canvas.tag_raise("arrow")
        self._canvas.tag_raise("pieces")

    def receive_heatmap_result(self, result: EvalResult) -> None:
        if result.request_id != self._heatmap_request_id:
            return
        self._stop_throbber()

        by_square: dict = {}
        for uci, score in result.move_evals.items():
            move = chess.Move.from_uci(uci)
            val = score.white().score(mate_score=50000)
            if val is None:
                val = 0
            by_square.setdefault(move.from_square, []).append((move, val / 100.0))

        is_black = (self._board.turn == chess.BLACK)
        all_vals_flat = [v for moves in by_square.values() for _, v in moves]
        unique_sorted = sorted(set(all_vals_flat))
        n_unique = len(unique_sorted)
        val_to_t = {
            v: (i / (n_unique - 1) if n_unique > 1 else 0.5)
            for i, v in enumerate(unique_sorted)
        }
        if is_black:
            val_to_t = {v: 1.0 - t for v, t in val_to_t.items()}

        rank_map: dict = {}
        for sq_key, moves in by_square.items():
            for move, val in moves:
                rank_map[move.uci()] = val_to_t[val]
        self._rank_map = rank_map
        self._ply_rank_maps[self._current_ply] = rank_map
        self._ply_move_scores[self._current_ply] = dict(result.move_evals)

        self._heatmap_data = {
            sq: sorted(moves, key=lambda x: x[1], reverse=not is_black)
            for sq, moves in by_square.items()
        }

        # Determine best move and position eval
        if rank_map:
            best_uci = max(rank_map, key=lambda u: rank_map[u])
            self._best_move = chess.Move.from_uci(best_uci)
            # Position eval: score after the best move (from white's perspective)
            best_score_val = None
            for sq_key, moves in by_square.items():
                for move, val in moves:
                    if move.uci() == best_uci:
                        best_score_val = val * 100  # back to centipawns
                        break
            self._position_eval = best_score_val
        else:
            self._best_move = None
            self._position_eval = None

        if self._position_eval is not None:
            self._ply_position_evals[self._current_ply] = self._position_eval

        self._draw_eval_bar()

        if self._heatmap_mode:
            self._canvas.delete("heatmap")
            for sq, move_scores in self._heatmap_data.items():
                self._draw_heatmap_for_square(sq, move_scores, rank_map)
            self._canvas.tag_raise("eval")
            self._canvas.tag_raise("select")

        self._draw_best_move_arrow()
        self._canvas.tag_raise("arrow")
        self._canvas.tag_raise("pieces")

        # Refresh history colors now that rank_map is populated
        if self._history_callback:
            self._history_callback()
        if self.on_heatmap_done:
            self.on_heatmap_done()

    def receive_partial_heatmap(self, result: EvalResult) -> None:
        """Called after each individual move is evaluated; updates colors incrementally."""
        if result.request_id != self._heatmap_request_id:
            return

        by_square: dict = {}
        for uci, score in result.move_evals.items():
            move = chess.Move.from_uci(uci)
            val = score.white().score(mate_score=50000)
            if val is None:
                val = 0
            by_square.setdefault(move.from_square, []).append((move, val / 100.0))

        is_black = (self._board.turn == chess.BLACK)
        all_vals_flat = [v for moves in by_square.values() for _, v in moves]
        unique_sorted = sorted(set(all_vals_flat))
        n_unique = len(unique_sorted)
        val_to_t = {
            v: (i / (n_unique - 1) if n_unique > 1 else 0.5)
            for i, v in enumerate(unique_sorted)
        }
        if is_black:
            val_to_t = {v: 1.0 - t for v, t in val_to_t.items()}

        rank_map: dict = {}
        for sq_key, moves in by_square.items():
            for move, val in moves:
                rank_map[move.uci()] = val_to_t[val]
        self._rank_map = rank_map  # update so hover evals use current colors

        if self._heatmap_mode:
            heatmap_data = {
                sq: sorted(moves, key=lambda x: x[1], reverse=not is_black)
                for sq, moves in by_square.items()
            }
            self._canvas.delete("heatmap")
            for sq, move_scores in heatmap_data.items():
                self._draw_heatmap_for_square(sq, move_scores, rank_map)
            self._canvas.tag_raise("eval")
            self._canvas.tag_raise("select")
            self._canvas.tag_raise("arrow")
            self._canvas.tag_raise("pieces")

    # ── Navigation ────────────────────────────────────────────────────────────

    def _goto_ply(self, ply: int) -> None:
        ply = max(0, min(ply, len(self._game_moves)))
        self._current_ply = ply
        board = self._game_start_board.copy()
        for move in self._game_moves[:ply]:
            board.push(move)
        self._board = board
        self._best_move = None
        self._position_eval = None
        self._selected_squares = set()
        self._legal_from_selected = []
        self._hovered_square = None
        self._current_request_id += 1
        self._heatmap_data = {}
        self._rank_map = {}
        self._canvas.delete("eval")
        self._canvas.delete("select")
        self._canvas.delete("lastmove")
        self._canvas.delete("heatmap")
        self._canvas.delete("arrow")
        self._canvas.delete("pv_arrow")
        self._draw_last_move_highlight()
        self._draw_pieces()
        self._draw_eval_bar()
        self._request_heatmap()
        if self._history_callback:
            self._history_callback()

    # ── Press / drag / release ────────────────────────────────────────────────

    def _on_press(self, event: tk.Event) -> None:
        if self._animating:
            return
        sq = self._pixel_to_square(event.x, event.y)
        if sq is None:
            return
        self._drag_press = (event.x, event.y)
        self._drag_pos = (event.x, event.y)
        piece = self._board.piece_at(sq)
        if piece is not None:
            if any(True for m in self._board.legal_moves if m.from_square == sq):
                self._drag_from = sq
                img = self._piece_images.get(_piece_key(piece))
                if img is not None:
                    self._canvas.create_image(event.x, event.y, image=img, tags="drag")
                else:
                    glyph = PIECE_GLYPHS[piece.piece_type][piece.color]
                    piece_font = ("Segoe UI Symbol", max(12, int(self._sq * 0.5)))
                    self._canvas.create_text(
                        event.x + 1, event.y + 1,
                        text=glyph, font=piece_font,
                        fill="#444444" if piece.color == chess.WHITE else "#CCCCCC",
                        tags="drag",
                    )
                    self._canvas.create_text(
                        event.x, event.y,
                        text=glyph, font=piece_font,
                        fill="#FFFFFF" if piece.color == chess.WHITE else "#111111",
                        tags="drag",
                    )
                self._canvas.tag_raise("drag")

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_from is None:
            return
        dx = event.x - self._drag_pos[0]
        dy = event.y - self._drag_pos[1]
        self._canvas.move("drag", dx, dy)
        self._drag_pos = (event.x, event.y)  # update incremental pos only

        # Highlight the square under the cursor
        self._canvas.delete("drag_target")
        to_sq = self._pixel_to_square(event.x, event.y)
        if to_sq is not None and to_sq != self._drag_from:
            legal_targets = {m.to_square for m in self._board.legal_moves
                             if m.from_square == self._drag_from}
            if to_sq in legal_targets:
                x, y = self._sq_to_pixel(to_sq)
                self._canvas.create_rectangle(
                    x + 2, y + 2, x + self._sq - 2, y + self._sq - 2,
                    outline="#FFD700", width=3, tags="drag_target",
                )
                self._canvas.tag_raise("drag_target")
        self._canvas.tag_raise("drag")

    def _on_release(self, event: tk.Event) -> None:
        self._canvas.delete("drag_target")
        drag_from = self._drag_from
        self._drag_from = None

        dx = abs(event.x - self._drag_press[0])
        dy = abs(event.y - self._drag_press[1])
        dragged = self._canvas.find_withtag("drag")
        self._canvas.delete("drag")

        # Restore piece layer after drag (drag glyph is now removed)
        if drag_from is not None:
            self._draw_pieces()

        to_sq = self._pixel_to_square(event.x, event.y)

        if drag_from is not None and (dx > 4 or dy > 4) and to_sq is not None:
            # Drag drop — try to execute the move (no animation, drag was the animation)
            self._try_move(drag_from, to_sq, animated=False)
        elif drag_from is not None:
            # Short press — treat as click on the source square
            self._handle_click(drag_from)
        elif dragged == () or drag_from is None:
            # Clicked on empty space or non-draggable square
            if to_sq is not None:
                self._handle_click(to_sq)

    def _try_move(self, from_sq: chess.Square, to_sq: chess.Square, animated: bool = True) -> None:
        """Attempt to execute a move from from_sq to to_sq."""
        legal = [m for m in self._board.legal_moves
                 if m.from_square == from_sq and m.to_square == to_sq]
        if not legal:
            return
        move = legal[0]
        piece = self._board.piece_at(from_sq)
        if piece is not None and piece.piece_type == chess.PAWN \
                and chess.square_rank(to_sq) in (0, 7):
            promo = self._ask_promotion()
            if promo is None:
                return
            move = chess.Move(from_sq, to_sq, promotion=promo)
        self._execute_move(move, animated=animated)

    def _handle_click(self, sq: chess.Square) -> None:
        """Original click-to-select/move logic."""
        clicked_piece = self._board.piece_at(sq)
        dest_moves = [m for m in self._legal_from_selected if m.to_square == sq]

        if dest_moves:
            move = dest_moves[0]
            piece = self._board.piece_at(move.from_square)
            if piece is not None and piece.piece_type == chess.PAWN \
                    and chess.square_rank(move.to_square) in (0, 7):
                promo = self._ask_promotion()
                if promo is None:
                    return
                move = chess.Move(move.from_square, move.to_square, promotion=promo)
            self._execute_move(move)
        elif sq in self._selected_squares:
            self._selected_squares.discard(sq)
            self._legal_from_selected = [
                m for m in self._board.legal_moves
                if m.from_square in self._selected_squares
            ]
            self._draw_selection_overlays()
        elif (
            clicked_piece is not None
            and self._selected_squares
            and clicked_piece.color == self._board.piece_at(
                next(iter(self._selected_squares))
            ).color
        ):
            self._selected_squares.add(sq)
            self._legal_from_selected = [
                m for m in self._board.legal_moves
                if m.from_square in self._selected_squares
            ]
            self._draw_selection_overlays()
        elif clicked_piece is not None and any(
            True for m in self._board.legal_moves if m.from_square == sq
        ):
            self._selected_squares = {sq}
            self._legal_from_selected = [
                m for m in self._board.legal_moves if m.from_square == sq
            ]
            self._draw_selection_overlays()
        else:
            self._deselect()

    def _deselect(self) -> None:
        self._selected_squares = set()
        self._legal_from_selected = []
        self._canvas.delete("select")

    def _execute_move(self, move: chess.Move, animated: bool = True) -> None:
        if animated and not self._animating:
            piece = self._board.piece_at(move.from_square)
            if piece is not None:
                self._animating = True
                self._canvas.delete("select")
                self._animate_move(move, piece, on_complete=lambda: self._finish_execute(move))
                return
        self._finish_execute(move)

    def _finish_execute(self, move: chess.Move) -> None:
        self._animating = False
        # Truncate future moves if navigating mid-history
        self._game_moves = self._game_moves[:self._current_ply]
        self._game_moves.append(move)
        # Purge cached rank_maps for any now-invalid future plies
        for k in [k for k in self._ply_rank_maps if k > self._current_ply]:
            del self._ply_rank_maps[k]
        self._current_ply += 1

        self._board.push(move)
        self._best_move = None
        self._position_eval = None
        self._selected_squares = set()
        self._legal_from_selected = []
        self._hovered_square = None
        self._current_request_id += 1
        self._heatmap_data = {}
        self._rank_map = {}
        self._canvas.delete("eval")
        self._canvas.delete("select")
        self._canvas.delete("lastmove")
        self._canvas.delete("heatmap")
        self._canvas.delete("arrow")
        self._canvas.delete("pv_arrow")
        self._draw_last_move_highlight()
        self._draw_pieces()
        self._draw_eval_bar()
        self._request_heatmap()
        if self._history_callback:
            self._history_callback()

    def _animate_move(self, move: chess.Move, piece: chess.Piece, on_complete) -> None:
        """Slide the piece from source to destination over 8 frames."""
        x1, y1 = self._sq_to_pixel(move.from_square)
        x2, y2 = self._sq_to_pixel(move.to_square)
        cx1 = float(x1 + self._sq // 2)
        cy1 = float(y1 + self._sq // 2)
        cx2 = float(x2 + self._sq // 2)
        cy2 = float(y2 + self._sq // 2)
        frames = 8
        frame_ms = 30

        img = self._piece_images.get(_piece_key(piece))
        if img is not None:
            self._canvas.create_image(int(cx1), int(cy1), image=img, tags="anim")
        else:
            # Single text item during animation (no shadow — it's a 240ms slide)
            glyph = PIECE_GLYPHS[piece.piece_type][piece.color]
            piece_font = ("Segoe UI Symbol", max(12, int(self._sq * 0.5)))
            self._canvas.create_text(
                int(cx1), int(cy1) + 1, text=glyph, font=piece_font,
                fill="#FFFFFF" if piece.color == chess.WHITE else "#111111", tags="anim",
            )
        self._canvas.tag_raise("anim")

        def step(f: int) -> None:
            if f > frames:
                self._canvas.delete("anim")
                on_complete()
                return
            t = f / frames
            # Ease-out cubic
            t_ease = 1 - (1 - t) ** 3
            cx = cx1 + (cx2 - cx1) * t_ease
            cy = cy1 + (cy2 - cy1) * t_ease
            self._canvas.coords("anim", int(cx), int(cy))
            self._canvas.after(frame_ms, lambda: step(f + 1))

        step(1)

    def _ask_promotion(self) -> Optional[chess.PieceType]:
        result: list[Optional[chess.PieceType]] = [None]
        win = tk.Toplevel(self._canvas.winfo_toplevel())
        win.title("Promote pawn")
        win.resizable(False, False)
        win.grab_set()
        win.configure(bg="#3C3F41")
        tk.Label(
            win, text="Choose promotion piece:",
            bg="#3C3F41", fg="#EEEEEE", font=("Consolas", 11),
        ).pack(pady=(12, 6))
        btn_frame = tk.Frame(win, bg="#3C3F41")
        btn_frame.pack(pady=(0, 12), padx=16)
        for piece_type, label in PROMO_PIECES:
            def make_cb(pt=piece_type):
                def cb():
                    result[0] = pt
                    win.destroy()
                return cb
            tk.Button(
                btn_frame, text=label, width=8, command=make_cb(),
                bg="#4C5052", fg="#EEEEEE", activebackground="#5C6366",
            ).pack(side="left", padx=4)
        win.wait_window()
        return result[0]

    # ── Selection overlays ────────────────────────────────────────────────────

    def _draw_selection_overlays(self) -> None:
        self._canvas.delete("select")
        if not self._selected_squares:
            return
        for sel_sq in self._selected_squares:
            x, y = self._sq_to_pixel(sel_sq)
            self._canvas.create_rectangle(
                x + 2, y + 2, x + self._sq - 2, y + self._sq - 2,
                outline=SEL_BORDER, width=3, tags="select",
            )
        seen_dests: set = set()
        for move in self._legal_from_selected:
            if move.to_square in seen_dests:
                continue
            seen_dests.add(move.to_square)
            dx, dy = self._sq_to_pixel(move.to_square)
            cx = dx + self._sq // 2
            cy = dy + self._sq // 2
            r = 10
            if move.uci() in self._rank_map:
                t = self._rank_map[move.uci()]
                dot_color = "#FFD700" if t >= 1.0 else _score_to_color(t)
            else:
                dot_color = SEL_DOT
            self._canvas.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                fill=dot_color, outline="", tags="select",
            )
        self._canvas.tag_raise("pieces")

    def _draw_last_move_highlight(self) -> None:
        self._canvas.delete("lastmove")
        if not self._board.move_stack:
            return
        last = self._board.peek()
        # From-square: subtle (gray25 stipple)
        x, y = self._sq_to_pixel(last.from_square)
        self._canvas.create_rectangle(
            x, y, x + self._sq, y + self._sq,
            fill=LAST_MOVE_COLOR, outline="", stipple="gray25", tags="lastmove",
        )
        # To-square: more opaque (gray50) + bright border — visually distinct
        x, y = self._sq_to_pixel(last.to_square)
        self._canvas.create_rectangle(
            x, y, x + self._sq, y + self._sq,
            fill=LAST_MOVE_COLOR, outline="", stipple="gray50", tags="lastmove",
        )
        self._canvas.create_rectangle(
            x + 2, y + 2, x + self._sq - 2, y + self._sq - 2,
            fill="", outline=LAST_MOVE_TO_BORDER, width=3, tags="lastmove",
        )

    # ── Board drawing ─────────────────────────────────────────────────────────

    def _draw_board(self) -> None:
        # Outer border (dark)
        self._canvas.create_rectangle(
            OFFSET_X - 2, OFFSET_Y - 2,
            OFFSET_X + self._board_px + 2, OFFSET_Y + self._board_px + 2,
            outline="#111111", width=2, fill="", tags="board",
        )
        # Inner highlight border
        self._canvas.create_rectangle(
            OFFSET_X, OFFSET_Y,
            OFFSET_X + self._board_px, OFFSET_Y + self._board_px,
            outline="#555555", width=1, fill="", tags="board",
        )
        for sq in chess.SQUARES:
            col = chess.square_file(sq)
            row = chess.square_rank(sq)
            x, y = self._sq_to_pixel(sq)
            color = LIGHT_SQ if (col + row) % 2 == 0 else DARK_SQ
            self._canvas.create_rectangle(
                x, y, x + self._sq, y + self._sq,
                fill=color, outline="", tags="board",
            )

    def _draw_labels(self) -> None:
        """Draw coordinate labels outside the board — ranks left, files below."""
        label_font = ("Segoe UI", max(8, int(self._sq * 0.14)), "bold")
        label_color = "#AAAAAA"
        files = "hgfedcba" if self._flipped else "abcdefgh"
        ranks = list(range(1, 9)) if self._flipped else list(range(8, 0, -1))

        # Rank labels: centered vertically on each row, in the left margin strip
        label_x = OFFSET_X // 2
        for row in range(8):
            cy = OFFSET_Y + row * self._sq + self._sq // 2
            self._canvas.create_text(
                label_x, cy,
                anchor="center", text=str(ranks[row]),
                font=label_font, fill=label_color, tags="labels",
            )

        # File labels: centered horizontally on each column, in the bottom margin
        label_y = OFFSET_Y + self._board_px + 10
        for col in range(8):
            cx = OFFSET_X + col * self._sq + self._sq // 2
            self._canvas.create_text(
                cx, label_y,
                anchor="center", text=files[col],
                font=label_font, fill=label_color, tags="labels",
            )

    def _draw_pieces(self) -> None:
        self._canvas.delete("pieces")
        for sq in chess.SQUARES:
            piece = self._board.piece_at(sq)
            if piece is None:
                continue
            x, y = self._sq_to_pixel(sq)
            cx = x + self._sq // 2
            cy = y + self._sq // 2
            self._draw_piece_at(piece, cx, cy, "pieces")
        self._canvas.tag_raise("heatmap")
        self._canvas.tag_raise("eval")
        self._canvas.tag_raise("select")
        self._canvas.tag_raise("arrow")
        self._canvas.tag_raise("pieces")

    def _draw_piece_at(self, piece: chess.Piece, cx: int, cy: int, tag: str) -> None:
        """Draw a piece (image or glyph) centered at (cx, cy) with the given tag."""
        key = _piece_key(piece)
        img = self._piece_images.get(key)
        if img is not None:
            self._canvas.create_image(cx, cy, image=img, tags=tag)
        else:
            glyph = PIECE_GLYPHS[piece.piece_type][piece.color]
            piece_font = ("Segoe UI Symbol", max(12, int(self._sq * 0.5)))
            self._canvas.create_text(
                cx + 1, cy + 2,
                text=glyph, font=piece_font,
                fill="#444444" if piece.color == chess.WHITE else "#CCCCCC",
                tags=tag,
            )
            self._canvas.create_text(
                cx, cy + 1,
                text=glyph, font=piece_font,
                fill="#FFFFFF" if piece.color == chess.WHITE else "#111111",
                tags=tag,
            )

    # ── Eval bar (vertical, beside board) ─────────────────────────────────────

    def _draw_eval_bar(self) -> None:
        self._eval_canvas.delete("all")
        total_h = OFFSET_Y + self._board_px + 20

        # Background
        # Dark background
        self._eval_canvas.create_rectangle(
            0, 0, EVAL_BAR_W, total_h,
            fill="#111111", outline="",
        )

        if self._position_eval is not None:
            cp = self._position_eval
            fill_ratio = max(0.05, min(0.95, cp / 1200.0 + 0.5))
        else:
            fill_ratio = 0.5

        bar_top = OFFSET_Y
        bar_bot = OFFSET_Y + self._board_px
        bar_h = bar_bot - bar_top
        split_y = bar_top + int(bar_h * (1.0 - fill_ratio))

        # Gradient — black zone (top): #111 → #444
        slices = 40
        for i in range(slices):
            y0 = bar_top + i * (split_y - bar_top) // slices
            y1 = bar_top + (i + 1) * (split_y - bar_top) // slices
            t = i / max(slices - 1, 1)
            v = int(0x11 + (0x44 - 0x11) * t)
            color = f"#{v:02X}{v:02X}{v:02X}"
            self._eval_canvas.create_rectangle(
                2, y0, EVAL_BAR_W - 2, y1 + 1, fill=color, outline="",
            )

        # Gradient — white zone (bottom): #BBBBBB → #EFEFEF
        for i in range(slices):
            y0 = split_y + i * (bar_bot - split_y) // slices
            y1 = split_y + (i + 1) * (bar_bot - split_y) // slices
            t = i / max(slices - 1, 1)
            v = int(0xBB + (0xEF - 0xBB) * t)
            color = f"#{v:02X}{v:02X}{v:02X}"
            self._eval_canvas.create_rectangle(
                2, y0, EVAL_BAR_W - 2, y1 + 1, fill=color, outline="",
            )

        # Accent line at split
        self._eval_canvas.create_line(
            1, split_y, EVAL_BAR_W - 1, split_y, fill="#888888", width=1,
        )

        # Eval text — horizontal, centered in the dominant section
        if self._position_eval is not None:
            cp = self._position_eval
            if abs(cp) >= 50000:
                text = "M"
            elif abs(cp) > 900:
                text = f"{abs(cp) // 100}"
            else:
                text = f"{abs(cp / 100):.1f}"
            if fill_ratio > 0.5:
                text_y = bar_bot - 10
                text_color = "#111111"
            else:
                text_y = bar_top + 10
                text_color = "#EEEEEE"
            self._eval_canvas.create_text(
                EVAL_BAR_W // 2, text_y,
                text=text,
                font=("Segoe UI", max(6, int(self._sq * 0.09)), "bold"),
                fill=text_color,
            )

    # ── PV arrows + move explanation ──────────────────────────────────────────

    def show_pv(self, pv_uci: list) -> None:
        """Draw color-coded arrows for a PV sequence (list of UCI strings)."""
        self.clear_pv()
        board = self._board.copy()
        colors = ["#44EE88", "#EE6644", "#4499EE", "#AABBCC", "#888888"]
        widths = [7,          5,          4,          3,          2      ]
        shapes = [(16,20,8),  (14,18,7),  (12,16,6),  (10,14,5),  (8,12,4)]
        for i, uci in enumerate(pv_uci[:5]):
            try:
                move = chess.Move.from_uci(uci)
            except Exception:
                break
            if move not in board.legal_moves:
                break
            x1, y1 = self._sq_to_pixel(move.from_square)
            x2, y2 = self._sq_to_pixel(move.to_square)
            cx1 = x1 + self._sq // 2
            cy1 = y1 + self._sq // 2
            cx2 = x2 + self._sq // 2
            cy2 = y2 + self._sq // 2
            self._canvas.create_line(
                cx1, cy1, cx2, cy2,
                arrow=tk.LAST, arrowshape=shapes[i],
                width=widths[i], fill=colors[i], tags="pv_arrow",
            )
            board.push(move)
        self._canvas.tag_raise("pv_arrow")
        self._canvas.tag_raise("pieces")

    def clear_pv(self) -> None:
        self._canvas.delete("pv_arrow")

    @staticmethod
    def _get_move_explanation(board: chess.Board, move: chess.Move) -> str:
        """Return plain-English explanation of what a move accomplishes."""
        NAMES = {
            chess.KING: "King", chess.QUEEN: "Queen", chess.ROOK: "Rook",
            chess.BISHOP: "Bishop", chess.KNIGHT: "Knight", chess.PAWN: "Pawn",
        }
        VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                  chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 99}
        FILE_NAMES = "abcdefgh"
        CENTER = {chess.E4, chess.D4, chess.E5, chess.D5}
        parts = []

        mover = board.piece_at(move.from_square)
        target = board.piece_at(move.to_square)
        opponent = not board.turn

        # ── Special moves ──────────────────────────────────────────────────────
        if board.is_castling(move):
            side = "kingside" if board.is_kingside_castling(move) else "queenside"
            return f"Castles {side}, activates Rook"

        if board.is_en_passant(move):
            parts.append("En passant capture")

        if move.promotion:
            parts.append(f"Promotes to {NAMES[move.promotion]}")

        # ── Capture quality ────────────────────────────────────────────────────
        if target and not board.is_en_passant(move):
            attacker_val = VALUES.get(mover.piece_type, 0) if mover else 0
            target_val = VALUES.get(target.piece_type, 0)
            # Is target currently defended by opponent?
            is_hanging = not board.is_attacked_by(opponent, move.to_square)
            if is_hanging:
                parts.append(f"Takes free {NAMES[target.piece_type]}")
            elif target_val > attacker_val + 1:
                if mover and mover.piece_type == chess.ROOK and target_val == 3:
                    parts.append("Wins the exchange")
                else:
                    parts.append("Wins material")
            else:
                parts.append(f"Captures {NAMES[target.piece_type]}")
            # Removes the defender: did the captured piece protect another piece?
            for sq in board.attacks(move.to_square):
                protected = board.piece_at(sq)
                if protected and protected.color == board.turn:
                    # After capture, is this piece now less defended?
                    board_tmp = board.copy()
                    board_tmp.push(move)
                    if not board_tmp.is_attacked_by(opponent, sq):
                        parts.append(f"Removes defender of {NAMES[protected.piece_type]}")
                        break

        # ── Simulate the move ──────────────────────────────────────────────────
        board2 = board.copy()
        board2.push(move)

        # ── Check family ───────────────────────────────────────────────────────
        if board2.is_checkmate():
            parts.append("Checkmate!")
        elif board2.is_check():
            checkers = list(board2.checkers())
            if len(checkers) >= 2:
                parts.append("Double check!")
            elif checkers and move.to_square not in checkers:
                parts.append("Discovered check")
            else:
                parts.append("Gives check")

        # ── Tactical motifs ────────────────────────────────────────────────────
        if not target and not board2.is_check() and "En passant" not in " ".join(parts):
            attacked_pieces = [
                board2.piece_at(sq) for sq in board2.attacks(move.to_square)
                if board2.piece_at(sq) and board2.piece_at(sq).color == opponent
                and VALUES.get(board2.piece_at(sq).piece_type, 0) >= 3
            ]
            if len(attacked_pieces) >= 2:
                parts.append(
                    f"Forks {NAMES[attacked_pieces[0].piece_type]} "
                    f"and {NAMES[attacked_pieces[1].piece_type]}"
                )
            elif len(attacked_pieces) == 1:
                parts.append(f"Attacks {NAMES[attacked_pieces[0].piece_type]}")

        # Pin: an opponent piece is now pinned to its King
        if not board2.is_check():
            for sq in chess.SQUARES:
                p = board2.piece_at(sq)
                if p and p.color == opponent and p.piece_type != chess.KING:
                    if board2.is_pinned(opponent, sq):
                        # Was it already pinned before the move?
                        if not board.is_pinned(opponent, sq):
                            parts.append(f"Pins {NAMES[p.piece_type]} to the King")
                            break

        # Skewer: sliding piece attacks high-value piece, with another piece on the same ray behind it
        if mover and mover.piece_type in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            for sq in board2.attacks(move.to_square):
                front = board2.piece_at(sq)
                if front and front.color == opponent and VALUES.get(front.piece_type, 0) >= 5:
                    # Look along the same ray past sq
                    df = chess.square_file(sq) - chess.square_file(move.to_square)
                    dr = chess.square_rank(sq) - chess.square_rank(move.to_square)
                    if df != 0: df = df // abs(df)
                    if dr != 0: dr = dr // abs(dr)
                    behind_f = chess.square_file(sq) + df
                    behind_r = chess.square_rank(sq) + dr
                    if 0 <= behind_f <= 7 and 0 <= behind_r <= 7:
                        behind_sq = chess.square(behind_f, behind_r)
                        behind = board2.piece_at(behind_sq)
                        if behind and behind.color == opponent:
                            parts.append(
                                f"Skewers {NAMES[front.piece_type]} "
                                f"to win {NAMES[behind.piece_type]}"
                            )
                            break

        # ── Positional concepts ────────────────────────────────────────────────
        if mover:
            back_rank = 0 if board.turn == chess.WHITE else 7
            opp_half_min = 4 if board.turn == chess.WHITE else 0
            opp_half_max = 7 if board.turn == chess.WHITE else 3

            # Piece development
            if (mover.piece_type in (chess.KNIGHT, chess.BISHOP)
                    and chess.square_rank(move.from_square) == back_rank):
                parts.append(f"Develops {NAMES[mover.piece_type]}")

            # Outpost Knight
            if (mover.piece_type == chess.KNIGHT
                    and opp_half_min <= chess.square_rank(move.to_square) <= opp_half_max):
                dest_f = chess.square_file(move.to_square)
                dest_r = chess.square_rank(move.to_square)
                pawn_attack_rank = dest_r - 1 if board.turn == chess.WHITE else dest_r + 1
                can_be_attacked = False
                for af in (dest_f - 1, dest_f + 1):
                    if 0 <= af <= 7 and 0 <= pawn_attack_rank <= 7:
                        sq = chess.square(af, pawn_attack_rank)
                        p = board2.piece_at(sq)
                        if p and p.piece_type == chess.PAWN and p.color == opponent:
                            can_be_attacked = True
                            break
                if not can_be_attacked:
                    parts.append("Establishes outpost")

            # Controls center
            if move.to_square in CENTER and not target:
                parts.append("Controls center")

            # Opens file (pawn capture leaving the file clear)
            if target and target.piece_type == chess.PAWN:
                f = chess.square_file(move.to_square)
                file_pawns = [
                    board2.piece_at(chess.square(f, r))
                    for r in range(8)
                ]
                if not any(p and p.piece_type == chess.PAWN for p in file_pawns):
                    parts.append(f"Opens {FILE_NAMES[f]}-file")

            # Creates passed pawn (pawn move)
            if mover.piece_type == chess.PAWN and not target:
                f = chess.square_file(move.to_square)
                r = chess.square_rank(move.to_square)
                ranks_ahead = range(r + 1, 8) if board.turn == chess.WHITE else range(0, r)
                adjacent_files = [ff for ff in (f - 1, f, f + 1) if 0 <= ff <= 7]
                is_passed = not any(
                    board2.piece_at(chess.square(ff, rr)) and
                    board2.piece_at(chess.square(ff, rr)).piece_type == chess.PAWN and
                    board2.piece_at(chess.square(ff, rr)).color == opponent
                    for ff in adjacent_files for rr in ranks_ahead
                )
                if is_passed:
                    parts.append("Creates passed pawn")

        return "  ·  ".join(parts) if parts else "Improves position"

    @staticmethod
    def _explain_pv(board: chess.Board, pv_uci: list) -> str:
        """Return a multi-line explanation for each move in the PV sequence."""
        lines = []
        b = board.copy()
        for i, uci in enumerate(pv_uci[:5]):
            try:
                move = chess.Move.from_uci(uci)
            except Exception:
                break
            if move not in b.legal_moves:
                break
            dot = "." if b.turn == chess.WHITE else "..."
            label = f"{b.fullmove_number}{dot} {b.san(move)}"
            note = BoardUI._get_move_explanation(b, move)
            prefix = "▶ " if i == 0 else "  "
            lines.append(f"{prefix}{label}: {note}")
            b.push(move)
        return "\n".join(lines)

    # ── Best-move arrow ───────────────────────────────────────────────────────

    def _draw_best_move_arrow(self) -> None:
        self._canvas.delete("arrow")
        if not self._show_arrow or self._best_move is None:
            return
        move = self._best_move
        x1, y1 = self._sq_to_pixel(move.from_square)
        x2, y2 = self._sq_to_pixel(move.to_square)
        cx1 = x1 + self._sq // 2
        cy1 = y1 + self._sq // 2
        cx2 = x2 + self._sq // 2
        cy2 = y2 + self._sq // 2
        self._canvas.create_line(
            cx1, cy1, cx2, cy2,
            arrow=tk.LAST, arrowshape=(18, 22, 9),
            width=7, fill="#FFD700", tags="arrow",
        )
        self._canvas.tag_raise("arrow")
        self._canvas.tag_raise("pieces")

    # ── Eval overlay ──────────────────────────────────────────────────────────

    def _draw_eval_on_square(
        self,
        square: chess.Square,
        score: chess.engine.Score,
        border_color: str,
    ) -> None:
        x, y = self._sq_to_pixel(square)
        tag = "eval"

        raw = score.white().score(mate_score=50000)
        if raw is None:
            raw = 0
        clamped = max(-EVAL_MAX, min(EVAL_MAX, raw / 100.0))
        fill_ratio = (clamped + EVAL_MAX) / (2 * EVAL_MAX)
        bar_x = x + int(self._sq * fill_ratio)

        if self._display_mode in ("bar", "both"):
            self._canvas.create_rectangle(
                x, y, x + self._sq, y + EVAL_BAR_H,
                fill=BAR_BG, outline="", tags=tag,
            )
            self._canvas.create_rectangle(
                x, y, bar_x, y + EVAL_BAR_H,
                fill=BAR_WHITE, outline="", tags=tag,
            )
            mid_x = x + self._sq // 2
            self._canvas.create_line(
                mid_x, y, mid_x, y + EVAL_BAR_H,
                fill=BAR_CENTER, tags=tag,
            )

        if self._display_mode in ("number", "both"):
            if score.white().is_mate():
                m = score.white().mate()
                text = f"M{abs(m)}" if m is not None else "M?"
            else:
                v = raw / 100.0
                sign = "+" if v >= 0 else ""
                text = f"{sign}{v:.1f}"
            text_color = TEXT_BEST if border_color == HIGHLIGHT_BEST else TEXT_OTHER
            self._canvas.create_rectangle(
                x + self._sq - 32, y,
                x + self._sq, y + EVAL_BAR_H,
                fill="#000000", outline="", tags=tag,
            )
            self._canvas.create_text(
                x + self._sq - 2, y + 2,
                anchor="ne", text=text,
                font=EVAL_FONT, fill=text_color, tags=tag,
            )

        bar_bottom = y + EVAL_BAR_H if self._display_mode in ("bar", "both") else y
        self._canvas.create_rectangle(
            x + 1, bar_bottom,
            x + self._sq - 1, y + self._sq - 1,
            outline=border_color, width=2, tags=tag,
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
        self._canvas.delete("throbber")

    def _animate_throbber(self) -> None:
        if not self._throbber_active:
            return
        self._canvas.delete("throbber")

        n = 12
        # Sit in the bottom margin of the canvas, centered horizontally
        cx = OFFSET_X + self._board_px // 2
        cy = OFFSET_Y + self._board_px + 10
        r = 7       # orbit radius
        dot_r = 3   # dot radius

        for i in range(n):
            angle = math.radians(self._throbber_angle + i * 360 / n)
            x = cx + r * math.cos(angle)
            y = cy + r * math.sin(angle)
            t = i / (n - 1)
            color = "#FFD700" if t >= 1.0 else _score_to_color(t)
            # Tail dots are smaller to give a comet effect
            dr = dot_r * (0.35 + 0.65 * t)
            self._canvas.create_oval(
                x - dr, y - dr, x + dr, y + dr,
                fill=color, outline="", tags="throbber",
            )

        self._throbber_angle = (self._throbber_angle + 10) % 360
        self._canvas.after(40, self._animate_throbber)

    # ── Heatmap ───────────────────────────────────────────────────────────────

    def _request_heatmap(self) -> None:
        all_legal = list(self._board.legal_moves)
        if not all_legal:
            return
        self._start_throbber()
        # Sort by explorer frequency: most-played human moves first
        if self._explorer_move_order:
            order = {uci: i for i, uci in enumerate(self._explorer_move_order)}
            n = len(self._explorer_move_order)
            all_legal.sort(key=lambda m: order.get(m.uci(), n))
        self._heatmap_request_id += 1
        request = EvalRequest(
            request_id=self._heatmap_request_id,
            board=self._board.copy(),
            moves=all_legal,
            callback=self.receive_heatmap_result,
            depth=self._worker._depth,
            progress_callback=self.receive_partial_heatmap,
        )
        self._worker.submit_request(request)

    def _draw_heatmap_for_square(
        self, sq: chess.Square, move_scores: list, rank_map: dict,
    ) -> None:
        x, y = self._sq_to_pixel(sq)
        n = len(move_scores)
        if n == 0:
            return
        for i, (move, val) in enumerate(move_scores):
            t = rank_map.get(move.uci(), 0.5)
            color = "#FFD700" if t >= 1.0 else _score_to_color(t)
            by  = y + round(i * self._sq / n)
            by2 = y + round((i + 1) * self._sq / n)
            self._canvas.create_rectangle(
                x, by, x + self._sq, by2,
                fill=color, outline="", tags="heatmap",
            )

    # ── Hover logic ───────────────────────────────────────────────────────────

    def _on_mouse_move(self, event: tk.Event) -> None:
        sq = self._pixel_to_square(event.x, event.y)
        if sq == self._hovered_square:
            return
        self._hovered_square = sq
        self._canvas.delete("eval")
        self._last_moves = []
        if sq is None:
            return
        piece = self._board.piece_at(sq)
        if piece is None:
            return
        legal_from_sq = [m for m in self._board.legal_moves if m.from_square == sq]
        if not legal_from_sq:
            return
        self._last_moves = legal_from_sq
        self._request_evals_for_square(sq)

    def _on_mouse_leave(self, event: tk.Event) -> None:
        self._hovered_square = None
        self._last_moves = []
        self._current_request_id += 1
        self._canvas.delete("eval")
        self._request_heatmap()

    def _request_evals_for_square(self, square: chess.Square) -> None:
        legal = [m for m in self._board.legal_moves if m.from_square == square]
        if not legal:
            return
        self._current_request_id += 1
        request = EvalRequest(
            request_id=self._current_request_id,
            board=self._board.copy(),
            moves=legal,
            callback=self.receive_eval_result,
            depth=self._worker._depth,
        )
        self._worker.submit_request(request)

    # ── Coordinate helpers ────────────────────────────────────────────────────

    def _sq_to_pixel(self, square: chess.Square) -> tuple:
        col = chess.square_file(square)
        row = chess.square_rank(square)
        if self._flipped:
            col = 7 - col
            row = 7 - row
        x = OFFSET_X + col * self._sq
        y = OFFSET_Y + (7 - row) * self._sq
        return x, y

    def _pixel_to_square(self, px: int, py: int) -> Optional[chess.Square]:
        col = (px - OFFSET_X) // self._sq
        row = 7 - (py - OFFSET_Y) // self._sq
        if 0 <= col <= 7 and 0 <= row <= 7:
            if self._flipped:
                col = 7 - col
                row = 7 - row
            return chess.square(col, row)
        return None
