"""
board_ui.py
tkinter Canvas chess board with hover-triggered engine evaluation overlays,
click-to-move piece interaction, heatmap, eval bar, move history navigation,
board flip, and best-move arrow.
"""

import tkinter as tk
from typing import Callable, Dict, List, Optional

import chess
import chess.pgn

from engine_worker import EvalRequest, EvalResult, EngineWorker

# ── Layout constants ──────────────────────────────────────────────────────────
SQUARE_SIZE = 72          # px per square → 576 px board
OFFSET_X = 30             # left margin for rank labels
OFFSET_Y = 30             # top margin for file labels
BOARD_PX = SQUARE_SIZE * 8
EVAL_BAR_W = 24           # width of the vertical position eval bar

# ── Eval overlay constants ────────────────────────────────────────────────────
EVAL_BAR_H = 14           # px tall for the mini eval bar
EVAL_MAX = 5.0            # clamp score to ±5 pawns

# ── Colors ────────────────────────────────────────────────────────────────────
LIGHT_SQ = "#F0D9B5"
DARK_SQ  = "#B58863"
HIGHLIGHT_BEST  = "#FFD700"
HIGHLIGHT_OTHER = "#88BBFF"
BAR_BG    = "#333333"
BAR_WHITE = "#FFFFFF"
BAR_CENTER = "#888888"
TEXT_BEST  = "#FFD700"
TEXT_OTHER = "#FFFFFF"

SEL_BORDER   = "#00FF88"
SEL_DOT      = "#44FF88"
LAST_MOVE_COLOR = "#CC7700"

# ── Piece glyphs ──────────────────────────────────────────────────────────────
PIECE_GLYPHS: Dict[chess.PieceType, Dict[chess.Color, str]] = {
    chess.KING:   {chess.WHITE: "♔", chess.BLACK: "♚"},
    chess.QUEEN:  {chess.WHITE: "♕", chess.BLACK: "♛"},
    chess.ROOK:   {chess.WHITE: "♖", chess.BLACK: "♜"},
    chess.BISHOP: {chess.WHITE: "♗", chess.BLACK: "♝"},
    chess.KNIGHT: {chess.WHITE: "♘", chess.BLACK: "♞"},
    chess.PAWN:   {chess.WHITE: "♙", chess.BLACK: "♟"},
}

PIECE_FONT = ("Segoe UI Symbol", 36)
EVAL_FONT  = ("Consolas", 8, "bold")

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
    ) -> None:
        self._worker = engine_worker
        self._board = initial_board.copy()
        self._display_mode = display_mode
        self._history_callback = history_callback  # called after any navigation

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
        self._ply_rank_maps: dict = {}  # ply → rank_map for moves FROM that position

        # Drag state
        self._drag_from: Optional[chess.Square] = None
        self._drag_press: tuple = (0, 0)   # original press coords (for click/drag detection)
        self._drag_pos: tuple = (0, 0)     # last motion coords (for incremental canvas.move)

        total_w = OFFSET_X + BOARD_PX + 10
        total_h = OFFSET_Y + BOARD_PX + 20

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
        self._draw_last_move_highlight()
        self._draw_pieces()
        self._draw_eval_bar()
        self._request_heatmap()
        if self._history_callback:
            self._history_callback()

    # ── Press / drag / release ────────────────────────────────────────────────

    def _on_press(self, event: tk.Event) -> None:
        sq = self._pixel_to_square(event.x, event.y)
        if sq is None:
            return
        self._drag_press = (event.x, event.y)
        self._drag_pos = (event.x, event.y)
        piece = self._board.piece_at(sq)
        if piece is not None:
            if any(True for m in self._board.legal_moves if m.from_square == sq):
                self._drag_from = sq
                glyph = PIECE_GLYPHS[piece.piece_type][piece.color]
                # Shadow
                self._canvas.create_text(
                    event.x + 1, event.y + 1,
                    text=glyph, font=PIECE_FONT,
                    fill="#444444" if piece.color == chess.WHITE else "#CCCCCC",
                    tags="drag",
                )
                # Piece
                self._canvas.create_text(
                    event.x, event.y,
                    text=glyph, font=PIECE_FONT,
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
                    x + 2, y + 2, x + SQUARE_SIZE - 2, y + SQUARE_SIZE - 2,
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
            # Drag drop — try to execute the move
            self._try_move(drag_from, to_sq)
        elif drag_from is not None:
            # Short press — treat as click on the source square
            self._handle_click(drag_from)
        elif dragged == () or drag_from is None:
            # Clicked on empty space or non-draggable square
            if to_sq is not None:
                self._handle_click(to_sq)

    def _try_move(self, from_sq: chess.Square, to_sq: chess.Square) -> None:
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
        self._execute_move(move)

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

    def _execute_move(self, move: chess.Move) -> None:
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
        self._draw_last_move_highlight()
        self._draw_pieces()
        self._draw_eval_bar()
        self._request_heatmap()
        if self._history_callback:
            self._history_callback()

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
                x + 2, y + 2, x + SQUARE_SIZE - 2, y + SQUARE_SIZE - 2,
                outline=SEL_BORDER, width=3, tags="select",
            )
        seen_dests: set = set()
        for move in self._legal_from_selected:
            if move.to_square in seen_dests:
                continue
            seen_dests.add(move.to_square)
            dx, dy = self._sq_to_pixel(move.to_square)
            cx = dx + SQUARE_SIZE // 2
            cy = dy + SQUARE_SIZE // 2
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
        for sq in (last.from_square, last.to_square):
            x, y = self._sq_to_pixel(sq)
            self._canvas.create_rectangle(
                x, y, x + SQUARE_SIZE, y + SQUARE_SIZE,
                fill=LAST_MOVE_COLOR, outline="", stipple="gray25", tags="lastmove",
            )

    # ── Board drawing ─────────────────────────────────────────────────────────

    def _draw_board(self) -> None:
        for sq in chess.SQUARES:
            col = chess.square_file(sq)
            row = chess.square_rank(sq)
            x, y = self._sq_to_pixel(sq)
            color = LIGHT_SQ if (col + row) % 2 == 0 else DARK_SQ
            self._canvas.create_rectangle(
                x, y, x + SQUARE_SIZE, y + SQUARE_SIZE,
                fill=color, outline="", tags="board",
            )

    def _draw_labels(self) -> None:
        label_font = ("Consolas", 9)
        label_color = "#AAAAAA"
        files = "hgfedcba" if self._flipped else "abcdefgh"
        ranks = list(range(1, 9)) if self._flipped else list(range(8, 0, -1))
        for col in range(8):
            x = OFFSET_X + col * SQUARE_SIZE + SQUARE_SIZE // 2
            self._canvas.create_text(
                x, OFFSET_Y + BOARD_PX + 8,
                text=files[col],
                font=label_font, fill=label_color, tags="labels",
            )
        for row in range(8):
            y = OFFSET_Y + row * SQUARE_SIZE + SQUARE_SIZE // 2
            self._canvas.create_text(
                OFFSET_X - 12, y,
                text=str(ranks[row]),
                font=label_font, fill=label_color, tags="labels",
            )

    def _draw_pieces(self) -> None:
        self._canvas.delete("pieces")
        for sq in chess.SQUARES:
            piece = self._board.piece_at(sq)
            if piece is None:
                continue
            glyph = PIECE_GLYPHS[piece.piece_type][piece.color]
            x, y = self._sq_to_pixel(sq)
            cx = x + SQUARE_SIZE // 2
            cy = y + SQUARE_SIZE // 2 + 2
            self._canvas.create_text(
                cx + 1, cy + 1,
                text=glyph, font=PIECE_FONT,
                fill="#444444" if piece.color == chess.WHITE else "#CCCCCC",
                tags="pieces",
            )
            self._canvas.create_text(
                cx, cy,
                text=glyph, font=PIECE_FONT,
                fill="#FFFFFF" if piece.color == chess.WHITE else "#111111",
                tags="pieces",
            )
        self._canvas.tag_raise("heatmap")
        self._canvas.tag_raise("eval")
        self._canvas.tag_raise("select")
        self._canvas.tag_raise("arrow")
        self._canvas.tag_raise("pieces")

    # ── Eval bar (vertical, beside board) ─────────────────────────────────────

    def _draw_eval_bar(self) -> None:
        self._eval_canvas.delete("all")
        total_h = OFFSET_Y + BOARD_PX + 20

        # Background
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
        bar_bot = OFFSET_Y + BOARD_PX
        bar_h = bar_bot - bar_top

        split_y = bar_top + int(bar_h * (1.0 - fill_ratio))

        # Black portion (top)
        self._eval_canvas.create_rectangle(
            2, bar_top, EVAL_BAR_W - 2, split_y,
            fill="#1A1A1A", outline="",
        )
        # White portion (bottom)
        self._eval_canvas.create_rectangle(
            2, split_y, EVAL_BAR_W - 2, bar_bot,
            fill="#E8E8E8", outline="",
        )

        # Eval text — pinned to the far end of the dominant section
        if self._position_eval is not None:
            cp = self._position_eval
            if abs(cp) >= 50000:
                text = "M"
            elif abs(cp) > 900:
                text = f"{cp/100:+.0f}"
            else:
                text = f"{cp/100:+.1f}"
            if fill_ratio > 0.5:
                # White winning → white section is larger (bottom) → pin number to bottom
                text_y = bar_bot - 10
                text_color = "#111111"
            else:
                # Black winning → black section is larger (top) → pin number to top
                text_y = bar_top + 10
                text_color = "#EEEEEE"
            self._eval_canvas.create_text(
                EVAL_BAR_W // 2, text_y,
                text=text,
                font=("Consolas", 8, "bold"),
                fill=text_color,
                angle=90,
            )

    # ── Best-move arrow ───────────────────────────────────────────────────────

    def _draw_best_move_arrow(self) -> None:
        self._canvas.delete("arrow")
        if not self._show_arrow or self._best_move is None:
            return
        move = self._best_move
        x1, y1 = self._sq_to_pixel(move.from_square)
        x2, y2 = self._sq_to_pixel(move.to_square)
        cx1 = x1 + SQUARE_SIZE // 2
        cy1 = y1 + SQUARE_SIZE // 2
        cx2 = x2 + SQUARE_SIZE // 2
        cy2 = y2 + SQUARE_SIZE // 2
        self._canvas.create_line(
            cx1, cy1, cx2, cy2,
            arrow=tk.LAST, arrowshape=(16, 20, 8),
            width=6, fill="#FFD700", tags="arrow",
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
        bar_x = x + int(SQUARE_SIZE * fill_ratio)

        if self._display_mode in ("bar", "both"):
            self._canvas.create_rectangle(
                x, y, x + SQUARE_SIZE, y + EVAL_BAR_H,
                fill=BAR_BG, outline="", tags=tag,
            )
            self._canvas.create_rectangle(
                x, y, bar_x, y + EVAL_BAR_H,
                fill=BAR_WHITE, outline="", tags=tag,
            )
            mid_x = x + SQUARE_SIZE // 2
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
                x + SQUARE_SIZE - 32, y,
                x + SQUARE_SIZE, y + EVAL_BAR_H,
                fill="#000000", outline="", tags=tag,
            )
            self._canvas.create_text(
                x + SQUARE_SIZE - 2, y + 2,
                anchor="ne", text=text,
                font=EVAL_FONT, fill=text_color, tags=tag,
            )

        bar_bottom = y + EVAL_BAR_H if self._display_mode in ("bar", "both") else y
        self._canvas.create_rectangle(
            x + 1, bar_bottom,
            x + SQUARE_SIZE - 1, y + SQUARE_SIZE - 1,
            outline=border_color, width=2, tags=tag,
        )

    # ── Heatmap ───────────────────────────────────────────────────────────────

    def _request_heatmap(self) -> None:
        all_legal = list(self._board.legal_moves)
        if not all_legal:
            return
        self._heatmap_request_id += 1
        request = EvalRequest(
            request_id=self._heatmap_request_id,
            board=self._board.copy(),
            moves=all_legal,
            callback=self.receive_heatmap_result,
            depth=self._worker._depth,
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
            by  = y + round(i * SQUARE_SIZE / n)
            by2 = y + round((i + 1) * SQUARE_SIZE / n)
            self._canvas.create_rectangle(
                x, by, x + SQUARE_SIZE, by2,
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
        x = OFFSET_X + col * SQUARE_SIZE
        y = OFFSET_Y + (7 - row) * SQUARE_SIZE
        return x, y

    def _pixel_to_square(self, px: int, py: int) -> Optional[chess.Square]:
        col = (px - OFFSET_X) // SQUARE_SIZE
        row = 7 - (py - OFFSET_Y) // SQUARE_SIZE
        if 0 <= col <= 7 and 0 <= row <= 7:
            if self._flipped:
                col = 7 - col
                row = 7 - row
            return chess.square(col, row)
        return None
