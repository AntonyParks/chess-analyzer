"""
main.py
Chess Analysis Tool — entry point.
"""

import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Optional
import threading
import urllib.parse
import urllib.request
try:
    import winsound
    _WINSOUND = True
except ImportError:
    _WINSOUND = False

import chess
import chess.pgn

from stockfish_setup import StockfishSetup
from pieces_setup import PiecesSetup
from engine_worker import EngineWorker
from board_ui import BoardUI, _score_to_color
from player_analysis import OpeningAnalysisWorker, NotableMove

# ── Opening explorer filter constants (shared by explorer + player analysis) ──
_SPEEDS        = ["ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence"]
_SPEED_LABELS  = ["Ultra", "Bullet", "Blitz", "Rapid", "Classical", "Corr."]
_RATINGS       = [1600, 1800, 2000, 2200, 2500]
_DEFAULT_SPEEDS  = {"bullet", "blitz", "rapid"}
_DEFAULT_RATINGS = {1600, 1800, 2000, 2200}


class ChessAnalyzerApp:
    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._root.title("Chess Analysis Tool")
        self._root.resizable(True, True)
        self._root.minsize(920, 700)
        self._root.configure(bg="#2B2B2B")
        self._root.after(120, lambda: self._root.state("zoomed"))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#3C3F41", foreground="#CCCCCC",
                         fieldbackground="#45494A", font=("Segoe UI", 10))
        style.configure("TLabel",        background="#3C3F41", foreground="#CCCCCC",
                         font=("Segoe UI", 10))
        style.configure("TButton",       background="#4C5052", foreground="#EEEEEE",
                         font=("Segoe UI", 10))
        style.configure("TLabelframe",   background="#3C3F41", foreground="#CCCCCC")
        style.configure("TLabelframe.Label", background="#3C3F41", foreground="#CCCCCC",
                         font=("Segoe UI", 10, "bold"))
        style.configure("TEntry",   fieldbackground="#45494A", foreground="#EEEEEE",
                         font=("Segoe UI", 10))
        style.configure("TSpinbox", fieldbackground="#45494A", foreground="#EEEEEE",
                         font=("Segoe UI", 10))
        style.map("TButton", background=[("active", "#5C6366")])
        style.configure("Explorer.Treeview",
                         background="#FFFFFF", foreground="#111111",
                         fieldbackground="#FFFFFF", rowheight=22)
        style.configure("Explorer.Treeview.Heading",
                         background="#DDDDDD", foreground="#111111",
                         font=("Segoe UI", 9, "bold"))
        style.map("Explorer.Treeview",
                  background=[("selected", "#3A5A8A")],
                  foreground=[("selected", "#FFFFFF")])

        self._pgn_path: str = ""
        self._depth_var = tk.IntVar(value=15)
        self._mode_var = tk.StringVar(value="number")

        self._engine_worker: EngineWorker = None  # type: ignore
        self._board_ui: BoardUI = None  # type: ignore
        self._pieces_dir = None

        # Game analysis state
        self._analyzing_game: bool = False
        self._analyze_ply_idx: int = 0
        self._analyze_total: int = 0

        # Resize debounce
        self._resize_after_id = None

        # Sound: track previous ply to detect forward navigation
        self._last_sound_ply: int = 0

        # Latest opening name from explorer
        self._last_opening_str: str = ""

        # Engine Lines click state
        self._top_lines_data: list = []

        # Game review state
        self._game_review_entries: list = []

        # Opening analysis state
        self._opening_analysis_worker: Optional[OpeningAnalysisWorker] = None
        self._analysis_results: list = []
        self._notable_arrow_fen: Optional[str] = None
        self._notable_arrow_uci: Optional[str] = None

        self._setup_stockfish_then_launch()

    def _setup_stockfish_then_launch(self) -> None:
        progress_win = tk.Toplevel(self._root)
        progress_win.title("Setting up...")
        progress_win.geometry("380x80")
        progress_win.resizable(False, False)
        progress_win.configure(bg="#3C3F41")
        progress_win.grab_set()

        lbl = tk.Label(
            progress_win,
            text="Checking for Stockfish engine...",
            bg="#3C3F41", fg="#BBBBBB",
            font=("Consolas", 10),
        )
        lbl.pack(pady=20)

        def update_label(msg: str) -> None:
            self._root.after(0, lambda: lbl.configure(text=msg))

        def do_setup() -> None:
            try:
                setup = StockfishSetup(progress_callback=update_label)
                exe_path = setup.ensure_ready()
                pieces = PiecesSetup(progress_callback=update_label)
                pieces_dir = pieces.ensure_ready()  # None on failure — graceful fallback
                self._pieces_dir = pieces_dir
                self._root.after(0, lambda: self._finish_launch(exe_path, progress_win))
            except Exception as e:
                self._root.after(0, lambda err=e: self._on_setup_failure(err, progress_win))

        threading.Thread(target=do_setup, daemon=True).start()

    def _on_setup_failure(self, error: Exception, progress_win: tk.Toplevel) -> None:
        progress_win.destroy()
        messagebox.showerror(
            "Engine Error",
            f"Could not start Stockfish:\n{error}\n\n"
            "Place stockfish.exe manually inside the chess_analyzer/engines/ folder.",
        )
        self._root.destroy()

    def _finish_launch(self, exe_path: Path, progress_win: tk.Toplevel) -> None:
        progress_win.destroy()

        self._engine_worker = EngineWorker(
            stockfish_path=exe_path,
            root_widget=self._root,
            depth=self._depth_var.get(),
        )

        # Pack fixed-height bottom frames BEFORE top_frame so they get space
        self._status_frame = tk.Frame(self._root, bg="#252525", pady=3)
        self._status_frame.pack(side="bottom", fill="x", padx=4)

        # ── Main layout: [controls] left | [board] center | [history] right ────
        top_frame = tk.Frame(self._root, bg="#2B2B2B")
        top_frame.pack(side="top", fill="both", expand=True)

        # Controls panel — left of board
        self._controls_frame = tk.Frame(top_frame, bg="#3C3F41", padx=4, pady=4)
        self._controls_frame.pack(side="left", fill="y", padx=(4, 0), pady=4)
        self._build_load_panel(self._controls_frame)
        self._build_toggle_panel(self._controls_frame)

        board_frame = tk.Frame(top_frame, bg="#2B2B2B")
        board_frame.pack(side="left", fill="both", expand=True)

        # History panel (right of board)
        history_frame = tk.Frame(top_frame, bg="#3C3F41", padx=4, pady=4)
        history_frame.pack(side="left", fill="y", padx=(0, 4), pady=4)

        ttk.Label(history_frame, text="Move History",
                  font=("Segoe UI", 10, "bold")).pack(pady=(2, 4))

        # Scrollable button grid — avoids all tk.Text tag rendering/click issues on Windows
        scroll_container = tk.Frame(history_frame, bg="#2B2B2B")
        scroll_container.pack(fill="both", expand=True)

        self._hist_canvas = tk.Canvas(
            scroll_container, bg="#2B2B2B", highlightthickness=0, width=200,
        )
        hist_scrollbar = tk.Scrollbar(
            scroll_container, orient="vertical", command=self._hist_canvas.yview,
        )
        self._history_inner = tk.Frame(self._hist_canvas, bg="#2B2B2B")

        self._hist_canvas.create_window((0, 0), window=self._history_inner, anchor="nw")
        self._hist_canvas.configure(yscrollcommand=hist_scrollbar.set)
        self._history_inner.bind(
            "<Configure>",
            lambda e: self._hist_canvas.configure(
                scrollregion=self._hist_canvas.bbox("all")
            ),
        )
        self._hist_canvas.pack(side="left", fill="both", expand=True)
        hist_scrollbar.pack(side="right", fill="y")

        # Arrow key navigation even when history has focus
        for w in (self._hist_canvas, self._history_inner):
            w.bind("<Left>",  lambda e: self._board_ui and self._board_ui.navigate_prev())
            w.bind("<Right>", lambda e: self._board_ui and self._board_ui.navigate_next())

        # State for incremental color updates (avoids full rebuild on every heatmap)
        self._history_btn_data: list = []   # [(button, ply_0based, uci, san), ...]
        self._history_snapshot: tuple = (-1, -1)  # (move_count, current_ply) at last rebuild

        nav_frame = ttk.Frame(history_frame)
        nav_frame.pack(fill="x", pady=(4, 0))
        for text, cmd in [
            ("⏮", lambda: self._board_ui.navigate_first()),
            ("◀", lambda: self._board_ui.navigate_prev()),
            ("▶", lambda: self._board_ui.navigate_next()),
            ("⏭", lambda: self._board_ui.navigate_last()),
        ]:
            ttk.Button(nav_frame, text=text, width=4, command=cmd).pack(side="left", padx=2)

        self._board_ui = BoardUI(
            parent=board_frame,
            engine_worker=self._engine_worker,
            initial_board=chess.Board(),
            display_mode="number",
            history_callback=self._update_history_display,
            pieces_dir=self._pieces_dir,
        )
        self._board_ui.set_heatmap_mode(True)
        self._board_ui.set_show_arrow(False)
        # Hook for game analysis: advance to next ply after each position finishes
        self._board_ui.on_heatmap_done = self._on_position_analyzed

        # ── Eval graph (below board, same horizontal width) ───────────────────
        self._graph_canvas = tk.Canvas(
            board_frame, bg="#1A1A1A", highlightthickness=0, height=70,
        )
        self._graph_canvas.pack(side="bottom", fill="x", padx=4, pady=(2, 4))
        self._graph_canvas.bind("<Button-1>", self._on_graph_click)
        self._graph_canvas.bind("<Configure>", lambda e: self._draw_eval_graph())

        # ── Status bar content (frame already created and packed above) ─────────
        self._turn_var = tk.StringVar(value="White to move")
        self._state_var = tk.StringVar(value="")
        self._move_var = tk.StringVar(value="")
        self._opening_bar_var = tk.StringVar(value="")
        tk.Label(self._status_frame, textvariable=self._turn_var,
                 bg="#252525", fg="#EEEEEE", font=("Segoe UI", 10)).pack(side="left", padx=(8, 0))
        self._state_label = tk.Label(self._status_frame, textvariable=self._state_var,
                 bg="#252525", fg="#FF8844", font=("Segoe UI", 10, "bold"))
        self._state_label.pack(side="left", padx=4)
        tk.Label(self._status_frame, textvariable=self._move_var,
                 bg="#252525", fg="#9D9D9D", font=("Segoe UI", 10)).pack(side="left", padx=8)
        tk.Label(self._status_frame, textvariable=self._opening_bar_var,
                 bg="#252525", fg="#AAAAAA", font=("Segoe UI", 10, "italic")).pack(side="left", padx=4)

        # (controls already built in top_frame above)

        # Opening Explorer panel (hidden until toggled on)
        self._explorer_cache: dict = {}
        self._explorer_fetch_pending = None   # (fen, filter_key) tuple or None
        self._explorer_frame = tk.Frame(self._controls_frame, bg="#3C3F41", padx=6, pady=4)
        # Not packed yet — appears when Opening Explorer checkbox is enabled

        # Token input row
        config = self._load_config()
        self._token_var = tk.StringVar(value=config.get("lichess_token", ""))
        token_row = ttk.Frame(self._explorer_frame)
        token_row.pack(anchor="w", pady=(0, 0))
        ttk.Label(token_row, text="Lichess API Token:").pack(side="left")
        ttk.Entry(token_row, textvariable=self._token_var, width=30).pack(side="left", padx=4)
        ttk.Button(token_row, text="Save", command=self._save_token).pack(side="left")
        ttk.Label(
            self._explorer_frame,
            text="Get token at lichess.org/account/oauth/token",
            foreground="#9D9D9D",
        ).pack(anchor="w", pady=(0, 6))

        # ── Filters ──────────────────────────────────────────────────────────
        self._explorer_speed_vars:  dict = {}
        self._explorer_rating_vars: dict = {}

        speed_frame = ttk.Frame(self._explorer_frame)
        speed_frame.pack(anchor="w", pady=(0, 2))
        ttk.Label(speed_frame, text="Speed:", width=7).pack(side="left")
        for speed, label in zip(_SPEEDS, _SPEED_LABELS):
            var = tk.BooleanVar(value=speed in _DEFAULT_SPEEDS)
            self._explorer_speed_vars[speed] = var
            ttk.Checkbutton(
                speed_frame, text=label, variable=var,
                command=self._on_explorer_filter_change,
            ).pack(side="left", padx=(0, 2))

        rating_frame = ttk.Frame(self._explorer_frame)
        rating_frame.pack(anchor="w", pady=(0, 4))
        ttk.Label(rating_frame, text="Rating:", width=7).pack(side="left")
        for r in _RATINGS:
            var = tk.BooleanVar(value=r in _DEFAULT_RATINGS)
            self._explorer_rating_vars[r] = var
            ttk.Checkbutton(
                rating_frame, text=str(r), variable=var,
                command=self._on_explorer_filter_change,
            ).pack(side="left", padx=(0, 2))

        self._explorer_status_var = tk.StringVar(value="")
        ttk.Label(
            self._explorer_frame,
            textvariable=self._explorer_status_var,
            wraplength=280,
            justify="left",
        ).pack(anchor="w", fill="x")

        cols = ("move", "games", "white", "draw", "black", "elo")
        self._explorer_tree = ttk.Treeview(
            self._explorer_frame, columns=cols, show="headings", height=6,
            style="Explorer.Treeview",
        )
        self._explorer_col_specs = [
            ("move",  "Move",     70, "w"),
            ("games", "Games",   110, "e"),
            ("white", "White%",   65, "center"),
            ("draw",  "Draw%",    60, "center"),
            ("black", "Black%",   65, "center"),
            ("elo",   "Avg Elo",  70, "center"),
        ]
        self._explorer_sort_col: str = "games"
        self._explorer_sort_asc: bool = False
        self._explorer_move_data: list = []
        for col, heading, width, anchor in self._explorer_col_specs:
            self._explorer_tree.heading(
                col, text=heading,
                command=lambda c=col: self._sort_explorer(c),
            )
            self._explorer_tree.column(col, width=width, anchor=anchor, stretch=False)
        explorer_scroll = ttk.Scrollbar(
            self._explorer_frame, orient="vertical",
            command=self._explorer_tree.yview,
        )
        self._explorer_tree.configure(yscrollcommand=explorer_scroll.set)
        self._explorer_tree.bind("<Double-1>", self._on_explorer_double_click)
        self._explorer_tree.bind("<<TreeviewSelect>>", self._on_explorer_select)
        self._explorer_tree.pack(side="left", fill="x")
        explorer_scroll.pack(side="left", fill="y")

        # Engine Lines panel (left controls, below explorer)
        self._build_engine_lines_panel(self._controls_frame)

        # Arrow key navigation
        self._root.bind("<Left>",  lambda _: self._board_ui.navigate_prev())
        self._root.bind("<Right>", lambda _: self._board_ui.navigate_next())
        self._root.bind("<Home>",  lambda _: self._board_ui.navigate_first())
        self._root.bind("<End>",   lambda _: self._board_ui.navigate_last())

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root.bind("<Configure>", self._on_window_configure)

    # ── History display ───────────────────────────────────────────────────────

    def _update_history_display(self) -> None:
        if self._board_ui is None:
            return
        moves = self._board_ui._game_moves
        current_ply = self._board_ui._current_ply
        start_board = self._board_ui._game_start_board
        ply_rank_maps = self._board_ui._ply_rank_maps

        # Clear PV display on any navigation
        self._board_ui.clear_pv()
        if hasattr(self, "_pv_explain_var"):
            self._pv_explain_var.set("")
        if hasattr(self, "_top_lines_labels"):
            for lbl in self._top_lines_labels:
                lbl.configure(bg="#2B2B2B")

        # Status bar update
        self._update_status_bar()

        # Move sound: play on forward navigation
        if current_ply > self._last_sound_ply:
            self._play_move_sound()
        self._last_sound_ply = current_ply

        # Eval graph update
        self._draw_eval_graph()

        snapshot = (len(moves), current_ply)

        if snapshot == self._history_snapshot and self._history_btn_data:
            # Structure unchanged — just recolor buttons and blunder markers in-place
            for btn, ply, uci, san in self._history_btn_data:
                is_current = (ply + 1 == current_ply)
                t_val = ply_rank_maps.get(ply, {}).get(uci)
                move_color = (
                    ("#FFD700" if t_val >= 1.0 else _score_to_color(t_val))
                    if t_val is not None else "#AAAAAA"
                )
                marker = self._classify_move(ply + 1)
                btn.configure(
                    text=san + marker,
                    bg="#3A5A8A" if is_current else "#2B2B2B",
                    fg="#FFFFFF" if is_current else move_color,
                )
            return

        # ── Full rebuild ──────────────────────────────────────────────────────
        self._history_snapshot = snapshot
        self._history_btn_data = []

        for w in self._history_inner.winfo_children():
            w.destroy()

        board = start_board.copy()
        grid_row = 0
        current_btn = None

        for ply, move in enumerate(moves):
            move_num = board.fullmove_number
            is_white = (board.turn == chess.WHITE)
            san = board.san(move)
            is_current = (ply + 1 == current_ply)

            uci = move.uci()
            t_val = ply_rank_maps.get(ply, {}).get(uci)
            move_color = (
                ("#FFD700" if t_val >= 1.0 else _score_to_color(t_val))
                if t_val is not None else "#AAAAAA"
            )
            marker = self._classify_move(ply + 1)

            if is_white:
                tk.Label(
                    self._history_inner,
                    text=f"{move_num}.",
                    bg="#2B2B2B", fg="#9D9D9D",
                    font=("Segoe UI", 11),
                    anchor="e", width=3,
                ).grid(row=grid_row, column=0, sticky="ew", padx=(4, 0))
                col = 1
            else:
                col = 2

            btn = tk.Button(
                self._history_inner,
                text=san + marker,
                bg="#3A5A8A" if is_current else "#2B2B2B",
                fg="#FFFFFF" if is_current else move_color,
                font=("Segoe UI", 11),
                relief="flat", bd=0, padx=4,
                cursor="hand2",
                activebackground="#4A6A9A",
                activeforeground="#FFFFFF",
                anchor="w",
                command=lambda p=ply + 1: self._board_ui.navigate_to_ply(p),
            )
            btn.grid(row=grid_row, column=col, sticky="ew", padx=1, pady=1)
            self._history_btn_data.append((btn, ply, uci, san))

            if is_current:
                current_btn = btn
            if not is_white:
                grid_row += 1

            board.push(move)

        # Scroll to show current move
        if current_btn is not None:
            self._history_inner.update_idletasks()
            btn_y = current_btn.winfo_y()
            canvas_h = self._hist_canvas.winfo_height()
            total_h = self._history_inner.winfo_reqheight()
            if total_h > canvas_h:
                frac = max(0.0, min(1.0, (btn_y - canvas_h // 2) / total_h))
                self._hist_canvas.yview_moveto(frac)

        # Refresh opening explorer for the new position
        if hasattr(self, "_explorer_var") and self._explorer_var.get():
            self._refresh_explorer()

        # Derive engine lines from heatmap data (same source as board coloring)
        if not self._analyzing_game:
            self._derive_engine_lines()

        # Notable move arrow: show when board is at the flagged position
        if self._board_ui and self._notable_arrow_fen is not None:
            if self._board_ui.get_fen() == self._notable_arrow_fen:
                self._board_ui.show_notable_move(self._notable_arrow_uci)
            else:
                self._board_ui.clear_notable_move()

    # ── Control panels ────────────────────────────────────────────────────────

    def _build_load_panel(self, parent: tk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Load Position", padding=6)
        frame.pack(fill="x", padx=6, pady=4)

        fen_row = ttk.Frame(frame)
        fen_row.pack(fill="x", pady=2)
        ttk.Label(fen_row, text="FEN:").pack(side="left")
        self._fen_var = tk.StringVar()
        ttk.Entry(fen_row, textvariable=self._fen_var, width=44).pack(side="left", padx=4)
        ttk.Button(fen_row, text="Load FEN", command=self._load_fen).pack(side="left")

        pgn_row = ttk.Frame(frame)
        pgn_row.pack(fill="x", pady=2)
        ttk.Label(pgn_row, text="PGN:").pack(side="left")
        self._pgn_label_var = tk.StringVar(value="no file loaded")
        ttk.Label(pgn_row, textvariable=self._pgn_label_var, width=28, anchor="w").pack(
            side="left", padx=4
        )
        ttk.Button(pgn_row, text="Browse...", command=self._browse_pgn).pack(side="left")
        ttk.Button(pgn_row, text="Load PGN", command=self._load_pgn).pack(side="left", padx=4)

        opt_row1 = ttk.Frame(frame)
        opt_row1.pack(fill="x", pady=(2, 1))
        ttk.Button(opt_row1, text="Starting Position", command=self._load_start).pack(side="left")
        ttk.Button(opt_row1, text="Undo Move", command=self._undo_move).pack(side="left", padx=4)
        ttk.Button(opt_row1, text="Copy FEN", command=self._copy_fen).pack(side="left")
        ttk.Button(opt_row1, text="Save PGN", command=self._save_pgn).pack(side="left", padx=4)

        opt_row2 = ttk.Frame(frame)
        opt_row2.pack(fill="x", pady=(1, 2))
        ttk.Button(opt_row2, text="Save Image", command=self._save_board_image).pack(side="left")
        ttk.Button(opt_row2, text="Analyze Game", command=self._analyze_game).pack(side="left", padx=4)
        self._review_btn = ttk.Button(opt_row2, text="Review Game",
                                       command=self._show_game_review_window, state="disabled")
        self._review_btn.pack(side="left", padx=(0, 4))
        ttk.Label(opt_row2, text="Depth:").pack(side="left")
        spin = ttk.Spinbox(
            opt_row2,
            from_=5, to=25,
            textvariable=self._depth_var,
            width=4,
            command=self._on_depth_change,
        )
        spin.pack(side="left", padx=(2, 0))
        spin.bind("<Return>", lambda _: self._on_depth_change())

    def _build_toggle_panel(self, parent: tk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Eval Display", padding=6)
        frame.pack(fill="x", padx=6, pady=4)

        self._heatmap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame, text="Piece Heatmap",
            variable=self._heatmap_var,
            command=self._on_heatmap_change,
        ).pack(anchor="w", pady=1)

        self._arrow_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="Best Move Arrow",
            variable=self._arrow_var,
            command=self._on_arrow_change,
        ).pack(anchor="w", pady=1)

        self._flip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="⇅ Flip Board",
            variable=self._flip_var,
            command=self._on_flip_change,
        ).pack(anchor="w", pady=1)

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=4)

        self._explorer_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="Opening Explorer",
            variable=self._explorer_var,
            command=self._on_explorer_toggle,
        ).pack(anchor="w", pady=1)

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=4)

        ttk.Button(
            frame, text="Opening Insights...",
            command=self._open_insights_window,
        ).pack(anchor="w", pady=1)

    # ── Button handlers ───────────────────────────────────────────────────────

    def _load_fen(self) -> None:
        fen = self._fen_var.get().strip()
        if not fen:
            messagebox.showwarning("Empty FEN", "Paste a FEN string first.")
            return
        try:
            board = chess.Board(fen)
            self._last_opening_str = ""
            self._board_ui.load_board(board)
        except ValueError as e:
            messagebox.showerror("Invalid FEN", f"Could not parse FEN:\n{e}")

    def _browse_pgn(self) -> None:
        path = filedialog.askopenfilename(
            title="Open PGN File",
            filetypes=[("PGN files", "*.pgn"), ("All files", "*.*")],
        )
        if path:
            self._pgn_path = path
            self._pgn_label_var.set(Path(path).name)

    def _load_pgn(self) -> None:
        if not self._pgn_path:
            messagebox.showwarning("No File", "Browse for a PGN file first.")
            return
        try:
            with open(self._pgn_path, encoding="utf-8", errors="replace") as f:
                game = chess.pgn.read_game(f)
            if game is None:
                raise ValueError("No game found in file.")
            start_board = game.board()
            moves = list(game.mainline_moves())
            self._board_ui.load_game(start_board, moves)
            self._game_review_entries = []
            self._review_btn.configure(state="disabled")
        except Exception as e:
            messagebox.showerror("PGN Error", str(e))

    def _load_start(self) -> None:
        self._last_opening_str = ""
        self._board_ui.load_board(chess.Board())

    def _undo_move(self) -> None:
        if self._board_ui:
            self._board_ui.undo_move()

    def _copy_fen(self) -> None:
        if self._board_ui:
            fen = self._board_ui.get_fen()
            self._root.clipboard_clear()
            self._root.clipboard_append(fen)

    def _save_pgn(self) -> None:
        if not self._board_ui:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".pgn",
            filetypes=[("PGN files", "*.pgn"), ("All files", "*.*")],
        )
        if not path:
            return
        pgn_text = self._board_ui.get_pgn()
        with open(path, "w", encoding="utf-8") as f:
            f.write(pgn_text)

    def _on_depth_change(self) -> None:
        try:
            depth = int(self._depth_var.get())
            depth = max(5, min(25, depth))
            self._depth_var.set(depth)
            if self._engine_worker:
                self._engine_worker.set_depth(depth)
        except (ValueError, tk.TclError):
            pass

    def _on_mode_change(self) -> None:
        if self._board_ui:
            self._board_ui.set_display_mode(self._mode_var.get())

    def _on_heatmap_change(self) -> None:
        if self._board_ui:
            self._board_ui.set_heatmap_mode(self._heatmap_var.get())

    def _on_arrow_change(self) -> None:
        if self._board_ui:
            self._board_ui.set_show_arrow(self._arrow_var.get())

    def _on_flip_change(self) -> None:
        if self._board_ui:
            self._board_ui.set_flipped(self._flip_var.get())

    def _load_config(self) -> dict:
        path = Path(__file__).parent / "config.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_config(self, data: dict) -> None:
        path = Path(__file__).parent / "config.json"
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _save_token(self) -> None:
        token = self._token_var.get().strip()
        self._save_config({"lichess_token": token})
        # Clear cache so next refresh uses the new token
        self._explorer_cache.clear()
        self._explorer_fetch_pending = None
        if self._explorer_var.get():
            self._refresh_explorer()

    def _on_explorer_toggle(self) -> None:
        if self._explorer_var.get():
            self._explorer_frame.pack(
                fill="x", pady=(4, 0),
                before=self._engine_lines_frame,
            )
            self._refresh_explorer()
        else:
            self._explorer_frame.pack_forget()

    def _get_explorer_filter_key(self) -> tuple:
        speeds  = tuple(s for s, v in self._explorer_speed_vars.items()  if v.get())
        ratings = tuple(r for r, v in self._explorer_rating_vars.items() if v.get())
        return (speeds, ratings)

    def _on_explorer_filter_change(self) -> None:
        """Called when any speed/rating checkbox changes — clears cache and re-fetches."""
        if not self._board_ui:
            return
        fen = self._board_ui.get_fen()
        # Evict all cached entries for this FEN so new filters hit the API
        for key in [k for k in self._explorer_cache if k[0] == fen]:
            del self._explorer_cache[key]
        if self._explorer_var.get():
            self._refresh_explorer()

    def _refresh_explorer(self) -> None:
        if not self._board_ui:
            return
        token = self._token_var.get().strip()
        if not token:
            self._explorer_status_var.set(
                "Enter a Lichess API token above to use the Opening Explorer"
            )
            for row in self._explorer_tree.get_children():
                self._explorer_tree.delete(row)
            return
        fen = self._board_ui.get_fen()
        filter_key = self._get_explorer_filter_key()
        cache_key = (fen, filter_key)
        if cache_key in self._explorer_cache:
            self._populate_explorer(self._explorer_cache[cache_key], fen)
            return
        if self._explorer_fetch_pending == cache_key:
            return  # already fetching this position+filters
        self._explorer_fetch_pending = cache_key
        self._explorer_status_var.set("Loading...")
        for row in self._explorer_tree.get_children():
            self._explorer_tree.delete(row)
        speeds  = [s for s, v in self._explorer_speed_vars.items()  if v.get()]
        ratings = [r for r, v in self._explorer_rating_vars.items() if v.get()]
        threading.Thread(
            target=self._fetch_explorer,
            args=(fen, token, speeds, ratings, cache_key),
            daemon=True,
        ).start()

    def _fetch_explorer(self, fen: str, token: str, speeds: list, ratings: list,
                        cache_key: tuple) -> None:
        params = [
            ("fen", fen),
            ("topGames", 0),
            ("recentGames", 0),
            ("variant", "standard"),
        ]
        if speeds:
            params.append(("speeds", ",".join(speeds)))
        if ratings:
            params.append(("ratings", ",".join(str(r) for r in ratings)))
        url = "https://explorer.lichess.ovh/lichess?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": "ChessAnalyzerApp/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            self._explorer_cache[cache_key] = data
            self._root.after(0, lambda d=data, f=fen: self._populate_explorer(d, f))
        except Exception:
            self._root.after(0, lambda f=fen: self._on_explorer_fetch_error(f))

    def _populate_explorer(self, data: dict, fen: str) -> None:
        if not self._board_ui or self._board_ui.get_fen() != fen:
            return
        total_w = data.get("white", 0)
        total_d = data.get("draws", 0)
        total_b = data.get("black", 0)
        total = total_w + total_d + total_b
        # Extract opening name
        opening = data.get("opening")
        if opening:
            eco = opening.get("eco", "")
            name = opening.get("name", "")
            opening_prefix = f"{eco} · {name}   " if eco else (f"{name}   " if name else "")
        else:
            opening_prefix = ""
        self._last_opening_str = opening_prefix.rstrip()
        if hasattr(self, "_opening_bar_var"):
            self._opening_bar_var.set(self._last_opening_str)
        if total > 0:
            w_pct = 100 * total_w // total
            d_pct = 100 * total_d // total
            b_pct = 100 * total_b // total
            status = (
                f"{opening_prefix}{total:,} games  "
                f"White {w_pct}%  Draw {d_pct}%  Black {b_pct}%"
            )
        else:
            status = "No games found"
        self._explorer_status_var.set(status)
        self._explorer_move_data = data.get("moves", [])[:15]
        self._explorer_sort_col = "games"
        self._explorer_sort_asc = False
        self._render_explorer_rows()

    def _sort_explorer(self, col: str) -> None:
        if self._explorer_sort_col == col:
            self._explorer_sort_asc = not self._explorer_sort_asc
        else:
            self._explorer_sort_col = col
            self._explorer_sort_asc = col == "move"  # text cols default asc
        self._render_explorer_rows()

    def _render_explorer_rows(self) -> None:
        col = self._explorer_sort_col
        asc = self._explorer_sort_asc

        def sort_key(m):
            mw = m.get("white", 0)
            md = m.get("draws", 0)
            mb = m.get("black", 0)
            mt = mw + md + mb or 1
            if col == "move":
                return m.get("san", "")
            elif col == "games":
                return mt
            elif col == "white":
                return mw / mt
            elif col == "draw":
                return md / mt
            elif col == "black":
                return mb / mt
            elif col == "elo":
                return m.get("averageRating", 0)
            return 0

        rows = sorted(self._explorer_move_data, key=sort_key, reverse=not asc)

        # Update heading labels with sort indicator
        for c, heading, _, _ in self._explorer_col_specs:
            indicator = (" ▲" if asc else " ▼") if c == col else ""
            self._explorer_tree.heading(
                c, text=heading + indicator,
                command=lambda cc=c: self._sort_explorer(cc),
            )

        for row in self._explorer_tree.get_children():
            self._explorer_tree.delete(row)

        uci_order = []
        for m in rows:
            mw = m.get("white", 0)
            md = m.get("draws", 0)
            mb = m.get("black", 0)
            mt = mw + md + mb
            w_pct = f"{100 * mw // mt}%" if mt else "-"
            d_pct = f"{100 * md // mt}%" if mt else "-"
            b_pct = f"{100 * mb // mt}%" if mt else "-"
            elo = str(m.get("averageRating", "-"))
            self._explorer_tree.insert("", "end", values=(
                m.get("san", "?"),
                f"{mt:,}",
                w_pct, d_pct, b_pct,
                elo,
            ))
            uci_order.append(m.get("uci", ""))
        if self._board_ui:
            self._board_ui.set_explorer_move_order(uci_order)

    def _on_explorer_select(self, event) -> None:
        """Highlight the piece and destinations on the board when a row is selected."""
        if not self._board_ui:
            return
        sel = self._explorer_tree.selection()
        if not sel:
            self._board_ui._deselect()
            return
        san = self._explorer_tree.item(sel[0], "values")[0]
        try:
            move = self._board_ui._board.parse_san(san)
        except Exception:
            return
        sq = move.from_square
        self._board_ui._selected_squares = {sq}
        self._board_ui._legal_from_selected = [
            m for m in self._board_ui._board.legal_moves if m.from_square == sq
        ]
        self._board_ui._draw_selection_overlays()

    def _on_explorer_double_click(self, event) -> None:
        item = self._explorer_tree.identify_row(event.y)
        if not item or not self._board_ui:
            return
        san = self._explorer_tree.item(item, "values")[0]
        try:
            move = self._board_ui._board.parse_san(san)
        except Exception:
            return
        self._board_ui._execute_move(move)

    def _on_explorer_fetch_error(self, fen: str) -> None:
        if self._board_ui and self._board_ui.get_fen() == fen:
            if not self._token_var.get().strip():
                self._explorer_status_var.set(
                    "Enter a Lichess API token above to use the Opening Explorer"
                )
            else:
                self._explorer_status_var.set(
                    "Error fetching data — check your token or connection"
                )
            for row in self._explorer_tree.get_children():
                self._explorer_tree.delete(row)

    def _on_close(self) -> None:
        if self._engine_worker:
            self._engine_worker.shutdown()
        self._root.destroy()

    # ── Status bar ────────────────────────────────────────────────────────────

    def _update_status_bar(self) -> None:
        if not hasattr(self, "_turn_var") or self._board_ui is None:
            return
        board = self._board_ui._board
        turn = "White to move" if board.turn == chess.WHITE else "Black to move"
        self._turn_var.set(turn)
        ply = self._board_ui._current_ply
        move_num = board.fullmove_number
        self._move_var.set(f"Move {move_num}  ·  Ply {ply}")
        if board.is_checkmate():
            state = "✕ Checkmate"
            color = "#FF4444"
        elif board.is_stalemate():
            state = "= Stalemate"
            color = "#AAAAAA"
        elif board.is_insufficient_material():
            state = "½ Insufficient material"
            color = "#AAAAAA"
        elif board.can_claim_fifty_moves():
            state = "½ Fifty-move rule"
            color = "#AAAAAA"
        elif board.is_check():
            state = "⚑ Check!"
            color = "#FF8844"
        else:
            state = ""
            color = "#FF8844"
        self._state_var.set(state)
        self._state_label.configure(fg=color)
        self._opening_bar_var.set(self._last_opening_str)

    # ── Move sound ────────────────────────────────────────────────────────────

    def _ensure_move_sound(self) -> None:
        """Generate a percussive click WAV file for move sound on first use."""
        import wave, struct, math, tempfile
        if hasattr(self, "_move_sound_file"):
            return
        try:
            rate = 22050
            duration = 0.07  # 70ms
            frames = int(rate * duration)
            data = []
            for i in range(frames):
                t = i / rate
                env = math.exp(-t * 55)
                sample = env * (
                    0.5 * math.sin(2 * math.pi * 700 * t)
                    + 0.3 * math.sin(2 * math.pi * 1400 * t)
                    + 0.2 * math.sin(2 * math.pi * 350 * t)
                )
                data.append(max(-32767, min(32767, int(sample * 32767))))
            tmpfile = tempfile.mktemp(suffix=".wav")
            with wave.open(tmpfile, "w") as f:
                f.setnchannels(1)
                f.setsampwidth(2)
                f.setframerate(rate)
                f.writeframes(struct.pack(f"<{len(data)}h", *data))
            self._move_sound_file = tmpfile
        except Exception:
            self._move_sound_file = None

    def _play_move_sound(self) -> None:
        if _WINSOUND:
            try:
                self._ensure_move_sound()
                if self._move_sound_file:
                    winsound.PlaySound(
                        self._move_sound_file,
                        winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                    )
                else:
                    winsound.Beep(440, 45)
            except Exception:
                pass

    # ── Responsive resize ─────────────────────────────────────────────────────

    def _on_window_configure(self, event) -> None:
        if event.widget is not self._root:
            return
        if self._resize_after_id:
            self._root.after_cancel(self._resize_after_id)
        self._resize_after_id = self._root.after(80, self._apply_resize)

    def _apply_resize(self) -> None:
        self._resize_after_id = None
        if not self._board_ui or not hasattr(self, "_status_frame"):
            return
        try:
            status_h = self._status_frame.winfo_height()
            graph_h = self._graph_canvas.winfo_height()
            avail_h = self._root.winfo_height() - status_h - graph_h - 24
            new_sq = max(50, min(130, avail_h // 8))
            self._board_ui.resize(new_sq)
            if self._notable_arrow_fen and self._board_ui.get_fen() == self._notable_arrow_fen:
                self._board_ui.show_notable_move(self._notable_arrow_uci)
            self._draw_eval_graph()
        except Exception:
            pass

    # ── Eval graph ────────────────────────────────────────────────────────────

    def _draw_eval_graph(self) -> None:
        if not hasattr(self, "_graph_canvas") or not self._board_ui:
            return
        c = self._graph_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 10 or h < 10:
            return
        evals = self._board_ui._ply_position_evals
        moves = self._board_ui._game_moves
        total = len(moves) + 1
        c.create_rectangle(0, 0, w, h, fill="#1A1A1A", outline="")
        cy = h // 2
        c.create_line(0, cy, w, cy, fill="#333333")
        if total < 2 or not evals:
            # No data yet — draw a faint placeholder line
            c.create_line(0, cy, w, cy, fill="#444444", dash=(4, 4))
            return
        # White advantage fill (above center)
        pts_white = [0, cy]
        pts_black = [0, cy]
        prev_x, prev_y = None, None
        for ply in range(total):
            if ply not in evals:
                continue
            x = int(ply / (total - 1) * (w - 1))
            cp = max(-700, min(700, evals[ply]))
            y = int(cy - cp / 700.0 * (cy - 4))
            if prev_x is not None:
                # White advantage area (above center)
                c.create_polygon(
                    prev_x, cy, prev_x, min(prev_y, cy),
                    x, min(y, cy), x, cy,
                    fill="#666666", outline="", stipple="gray50",
                )
                # Black advantage area (below center)
                c.create_polygon(
                    prev_x, cy, prev_x, max(prev_y, cy),
                    x, max(y, cy), x, cy,
                    fill="#333333", outline="", stipple="gray50",
                )
                c.create_line(prev_x, prev_y, x, y, fill="#CCCCCC", width=2)
            prev_x, prev_y = x, y
        # Current ply marker
        cur = self._board_ui._current_ply
        if total > 1:
            cx = int(cur / (total - 1) * (w - 1))
            c.create_line(cx, 0, cx, h, fill="#F6F669", width=2)

    def _on_graph_click(self, event) -> None:
        if not self._board_ui:
            return
        moves = self._board_ui._game_moves
        total = len(moves) + 1
        if total < 2:
            return
        w = self._graph_canvas.winfo_width()
        ply = round(event.x / max(1, w - 1) * (total - 1))
        ply = max(0, min(total - 1, ply))
        self._board_ui.navigate_to_ply(ply)

    # ── Blunder / mistake markers ─────────────────────────────────────────────

    def _classify_move(self, ply: int) -> str:
        """Return annotation symbol for the move at this ply (1-indexed)."""
        if not self._board_ui:
            return ""
        evals = self._board_ui._ply_position_evals
        if ply not in evals or (ply - 1) not in evals:
            return ""
        prev_cp = evals[ply - 1]
        after_cp = evals[ply]
        # Determine whose turn it was: ply-1 is the position before the move
        start_white = self._board_ui._game_start_board.turn == chess.WHITE
        # At ply 0 it's the starting side to move; ply 1 means 1 move was played by starting side
        was_white = start_white if (ply - 1) % 2 == 0 else not start_white
        drop = (prev_cp - after_cp) if was_white else (after_cp - prev_cp)
        if drop >= 200:
            return "??"
        if drop >= 80:
            return "?"
        if drop >= 30:
            return "!?"
        return ""

    # ── Engine Lines panel ────────────────────────────────────────────────────

    def _build_engine_lines_panel(self, parent: tk.Frame) -> None:
        self._engine_lines_frame = ttk.LabelFrame(parent, text="Engine Lines", padding=6)
        self._engine_lines_frame.pack(fill="x", padx=6, pady=4)

        self._top_lines_labels: list = []
        for i in range(3):
            lbl = tk.Label(
                self._engine_lines_frame, text="", bg="#2B2B2B", fg="#CCCCCC",
                font=("Segoe UI", 11), anchor="w", padx=4, cursor="hand2",
            )
            lbl.pack(fill="x", pady=1)
            lbl.bind("<Button-1>", lambda e, idx=i: self._on_line_click(idx))
            self._top_lines_labels.append(lbl)

        self._pv_explain_var = tk.StringVar(value="")
        tk.Label(
            self._engine_lines_frame, textvariable=self._pv_explain_var,
            bg="#2B2B2B", fg="#88CC88", font=("Segoe UI", 10, "italic"),
            anchor="nw", justify="left", padx=4, wraplength=220,
        ).pack(fill="x", pady=(2, 0))

    def _derive_engine_lines(self) -> None:
        """Populate Engine Lines from heatmap move scores — same source as board coloring."""
        if not hasattr(self, "_top_lines_labels") or not self._board_ui:
            return
        ply = self._board_ui._current_ply
        board = self._board_ui._board
        move_scores = self._board_ui._ply_move_scores.get(ply, {})
        if not move_scores:
            for lbl in self._top_lines_labels:
                lbl.configure(text="", bg="#2B2B2B")
            if hasattr(self, "_pv_explain_var"):
                self._pv_explain_var.set("")
            self._top_lines_data = []
            return

        def sort_key(item):
            _, score = item
            v = score.pov(board.turn).score(mate_score=30000)
            return v if v is not None else -99999

        top = sorted(move_scores.items(), key=sort_key, reverse=True)[:3]
        lines = []
        for uci, score in top:
            try:
                move = chess.Move.from_uci(uci)
                san = board.san(move)
            except Exception:
                continue
            cp = score.white().score(mate_score=30000)
            lines.append({"san": san, "cp": cp, "pv_uci": [uci]})
        self._update_top_lines(lines)

    def _request_top_lines(self) -> None:
        """Kept for on-demand PV only (not called automatically)."""
        if not self._board_ui or not self._engine_worker:
            return
        board = self._board_ui._board.copy()
        depth = max(8, self._depth_var.get() - 4)
        self._engine_worker.submit_position_analysis(board, depth, 3, self._update_top_lines)

    def _update_top_lines(self, lines: list) -> None:
        if not hasattr(self, "_top_lines_labels"):
            return
        self._top_lines_data = list(lines)
        for i, lbl in enumerate(self._top_lines_labels):
            if i < len(lines):
                info = lines[i]
                cp = info.get("cp")
                san = info.get("san", "")
                if cp is None:
                    eval_str = "?"
                elif abs(cp) >= 30000:
                    eval_str = "M"
                else:
                    sign = "+" if cp >= 0 else ""
                    eval_str = f"{sign}{cp / 100:.1f}"
                lbl.configure(text=f"{i + 1}. {san:<6} {eval_str}", bg="#2B2B2B")
            else:
                lbl.configure(text="", bg="#2B2B2B")
        if hasattr(self, "_pv_explain_var"):
            self._pv_explain_var.set("")

    def _on_line_click(self, idx: int) -> None:
        if not self._board_ui or idx >= len(self._top_lines_data):
            return
        data = self._top_lines_data[idx]
        pv_uci = data.get("pv_uci", [])

        # Highlight selected label, reset others
        for i, lbl in enumerate(self._top_lines_labels):
            lbl.configure(bg="#3A5A3A" if i == idx else "#2B2B2B")

        # Draw PV arrows on board
        self._board_ui.show_pv(pv_uci)

        # Generate 5-move PV narrative
        explanation = ""
        if pv_uci:
            try:
                cp = data.get("cp")
                eval_prefix = ""
                if cp is not None and abs(cp) < 30000:
                    sign = "+" if cp >= 0 else ""
                    eval_prefix = f"{sign}{cp / 100:.1f}\n"
                explanation = eval_prefix + BoardUI._explain_pv(self._board_ui._board, pv_uci)
            except Exception:
                explanation = ""
        if hasattr(self, "_pv_explain_var"):
            self._pv_explain_var.set(explanation)

    # ── Opening Insights popup ────────────────────────────────────────────────

    def _open_insights_window(self) -> None:
        if hasattr(self, "_insights_win") and self._insights_win.winfo_exists():
            self._insights_win.lift()
            self._insights_win.focus()
            return
        win = tk.Toplevel(self._root)
        win.title("Opening Insights")
        win.geometry("900x260")
        win.resizable(True, True)
        win.configure(bg="#2B2B2B")
        win.protocol("WM_DELETE_WINDOW", self._on_insights_close)
        self._insights_win = win
        self._build_opening_analysis_panel(win)

    def _on_insights_close(self) -> None:
        if self._opening_analysis_worker:
            self._opening_analysis_worker.stop()
        self._insights_win.destroy()

    # ── Opening Analysis panel (content) ──────────────────────────────────────

    def _build_opening_analysis_panel(self, parent: tk.Frame) -> None:
        """Full-width bar: controls on left, results treeview on right."""
        # Left: compact controls
        left_frame = tk.Frame(parent, bg="#3C3F41", padx=6, pady=4)
        left_frame.pack(side="left", fill="y")

        ttk.Label(left_frame, text="Opening Insights",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))

        row1 = ttk.Frame(left_frame)
        row1.pack(anchor="w", pady=(0, 2))
        ttk.Label(row1, text="Color:").pack(side="left")
        self._oa_color_var = tk.StringVar(value="white")
        ttk.Radiobutton(row1, text="White", variable=self._oa_color_var,
                        value="white").pack(side="left", padx=(2, 4))
        ttk.Radiobutton(row1, text="Black", variable=self._oa_color_var,
                        value="black").pack(side="left", padx=(0, 8))
        ttk.Label(row1, text="Min%:").pack(side="left")
        self._oa_minpct_var = tk.IntVar(value=5)
        ttk.Spinbox(row1, from_=1, to=50, textvariable=self._oa_minpct_var,
                    width=4).pack(side="left", padx=(2, 8))
        ttk.Label(row1, text="Min games:").pack(side="left")
        self._oa_mingames_var = tk.IntVar(value=100)
        ttk.Spinbox(row1, from_=0, to=100000, increment=100,
                    textvariable=self._oa_mingames_var,
                    width=6).pack(side="left", padx=(2, 8))
        ttk.Label(row1, text="Delta%:").pack(side="left")
        self._oa_delta_var = tk.IntVar(value=10)
        ttk.Spinbox(row1, from_=1, to=50, textvariable=self._oa_delta_var,
                    width=4).pack(side="left", padx=(2, 8))
        ttk.Label(row1, text="Top N:").pack(side="left")
        self._oa_topn_var = tk.IntVar(value=0)
        ttk.Spinbox(row1, from_=0, to=10, textvariable=self._oa_topn_var,
                    width=3).pack(side="left", padx=2)

        row2 = ttk.Frame(left_frame)
        row2.pack(anchor="w", pady=(0, 2))
        ttk.Label(row2, text="Speed:", width=6).pack(side="left")
        self._oa_speed_vars: dict = {}
        for speed, label in zip(_SPEEDS, _SPEED_LABELS):
            var = tk.BooleanVar(value=speed in _DEFAULT_SPEEDS)
            self._oa_speed_vars[speed] = var
            ttk.Checkbutton(row2, text=label, variable=var).pack(side="left", padx=(0, 2))

        row_r = ttk.Frame(left_frame)
        row_r.pack(anchor="w", pady=(0, 2))
        ttk.Label(row_r, text="Rating:", width=6).pack(side="left")
        self._oa_rating_vars: dict = {}
        for r in _RATINGS:
            var = tk.BooleanVar(value=r in _DEFAULT_RATINGS)
            self._oa_rating_vars[r] = var
            ttk.Checkbutton(row_r, text=str(r), variable=var).pack(side="left", padx=(0, 2))

        row3 = ttk.Frame(left_frame)
        row3.pack(anchor="w", pady=(2, 2))
        self._oa_run_btn = ttk.Button(row3, text="Analyze from Here",
                                      command=self._on_oa_start)
        self._oa_run_btn.pack(side="left")
        self._oa_stop_btn = ttk.Button(row3, text="Stop",
                                       command=self._on_oa_stop, state="disabled")
        self._oa_stop_btn.pack(side="left", padx=4)

        self._oa_status_var = tk.StringVar(value="")
        ttk.Label(left_frame, textvariable=self._oa_status_var,
                  wraplength=270, justify="left").pack(anchor="w", fill="x")

        # Explainer text
        _EXPLAINER = (
            "Win%  — win rate of this specific move\n"
            "Avg WR%  — weighted avg win rate across all moves at that position\n"
            "Delta  — Win% minus Avg WR% (how much this move outperforms)\n"
            "Popularity  — rank by game count  (#1 = most played)\n"
            "Top N  — limit opponent's responses to their N most popular moves (0 = all)"
        )
        ttk.Label(
            left_frame, text=_EXPLAINER,
            font=("Segoe UI", 8), justify="left", wraplength=270,
            foreground="#999999",
        ).pack(anchor="w", pady=(6, 0), fill="x")

        # Right: results treeview
        right_frame = tk.Frame(parent, bg="#2B2B2B")
        right_frame.pack(side="left", fill="both", expand=True, padx=(4, 0))

        self._oa_col_specs = [
            ("sequence",   "Position",   130, "w"),
            ("move",       "Move",        55, "w"),
            ("move_wr",    "Win%",        65, "center"),
            ("avg_wr",     "Avg WR%",     65, "center"),
            ("delta",      "Delta",       60, "center"),
            ("popularity", "Popularity",  80, "center"),
        ]
        self._oa_sort_col: str = "delta"
        self._oa_sort_asc: bool = False

        oa_cols = tuple(c for c, *_ in self._oa_col_specs)
        self._oa_tree = ttk.Treeview(
            right_frame, columns=oa_cols, show="headings", height=4,
            style="Explorer.Treeview",
        )
        for col, heading, width, anchor_val in self._oa_col_specs:
            self._oa_tree.column(col, width=width, anchor=anchor_val, stretch=False)
        self._oa_update_headings()
        oa_scroll = ttk.Scrollbar(right_frame, orient="vertical", command=self._oa_tree.yview)
        self._oa_tree.configure(yscrollcommand=oa_scroll.set)
        self._oa_tree.bind("<<TreeviewSelect>>", self._on_oa_row_select)
        self._oa_tree.pack(side="left", fill="both", expand=True)
        oa_scroll.pack(side="left", fill="y")

        self._oa_detail_var = tk.StringVar(value="")
        tk.Label(
            right_frame, textvariable=self._oa_detail_var,
            bg="#2B2B2B", fg="#CCCCCC", font=("Segoe UI", 10),
            anchor="w", justify="left", wraplength=600, pady=4,
        ).pack(side="top", fill="x", padx=4)

    def _on_oa_start(self) -> None:
        if not self._board_ui:
            return
        speeds = [s for s, v in self._oa_speed_vars.items() if v.get()]
        ratings = [r for r, v in self._oa_rating_vars.items() if v.get()]
        if not speeds:
            messagebox.showwarning("No Speeds", "Select at least one time control.")
            return

        for row in self._oa_tree.get_children():
            self._oa_tree.delete(row)
        self._analysis_results = []
        self._oa_sort_col = "delta"
        self._oa_sort_asc = False
        self._oa_update_headings()
        self._notable_arrow_fen = None
        self._notable_arrow_uci = None
        self._board_ui.clear_notable_move()

        if hasattr(self, "_oa_detail_var"):
            self._oa_detail_var.set("")
        self._oa_run_btn.configure(state="disabled")
        self._oa_stop_btn.configure(state="normal")
        self._oa_status_var.set("Starting analysis...")

        token = self._token_var.get().strip() if hasattr(self, "_token_var") else ""
        self._opening_analysis_worker = OpeningAnalysisWorker()
        self._opening_analysis_worker.start(
            color=self._oa_color_var.get(),
            min_pct=self._oa_minpct_var.get(),
            min_games=self._oa_mingames_var.get(),
            top_n=self._oa_topn_var.get(),
            speed_filters=speeds,
            rating_filters=ratings,
            delta_threshold=float(self._oa_delta_var.get()),
            start_fen=self._board_ui.get_fen(),
            token=token,
            on_progress=self._on_oa_progress,
            on_result=self._on_oa_result,
            on_complete=self._on_oa_complete,
            root=self._root,
        )

    def _on_oa_stop(self) -> None:
        if self._opening_analysis_worker:
            self._opening_analysis_worker.stop()
        self._oa_run_btn.configure(state="normal")
        self._oa_stop_btn.configure(state="disabled")
        self._oa_status_var.set("Stopped.")

    def _on_oa_progress(self, msg: str) -> None:
        self._oa_status_var.set(msg)

    def _on_oa_result(self, nm: NotableMove) -> None:
        self._analysis_results.append(nm)
        self._render_oa_rows()

    @staticmethod
    def _oa_seq_str(nm: NotableMove) -> str:
        board = chess.Board()
        san_seq = []
        for uci in nm.move_uci_sequence:
            try:
                move = chess.Move.from_uci(uci)
                san_seq.append(board.san(move))
                board.push(move)
            except Exception:
                san_seq.append(uci)
        if len(san_seq) > 4:
            return "..." + " ".join(san_seq[-4:])
        return " ".join(san_seq) if san_seq else "Start"

    def _oa_explain(self, nm: NotableMove) -> str:
        color_label = "White" if self._oa_color_var.get() == "white" else "Black"
        board = chess.Board()
        san_seq = []
        for uci in nm.move_uci_sequence:
            try:
                move = chess.Move.from_uci(uci)
                san_seq.append(board.san(move))
                board.push(move)
            except Exception:
                break
        pos_str = f"after {' '.join(san_seq)}" if san_seq else "from the starting position"

        if nm.popularity_rank == 1:
            surprise = " It is also the most-played move here."
        elif nm.popularity_rank == 2:
            surprise = f" It is the 2nd most popular move, yet outperforms the field by {nm.delta:.1f} pp."
        else:
            surprise = (
                f" Despite being only the #{nm.popularity_rank} most popular move, "
                f"it outperforms the field by {nm.delta:.1f} pp."
            )

        return (
            f"{pos_str.capitalize()}, {nm.san} scores {nm.move_wr:.1f}% for {color_label} — "
            f"{nm.delta:.1f} percentage points above the position average of "
            f"{nm.pos_avg_wr:.1f}%.{surprise}"
        )

    def _sort_oa(self, col: str) -> None:
        if self._oa_sort_col == col:
            self._oa_sort_asc = not self._oa_sort_asc
        else:
            self._oa_sort_col = col
            self._oa_sort_asc = col in ("sequence", "move", "popularity")
        self._render_oa_rows()

    def _oa_update_headings(self) -> None:
        for col, heading, _, _ in self._oa_col_specs:
            indicator = (" ▲" if self._oa_sort_asc else " ▼") if col == self._oa_sort_col else ""
            self._oa_tree.heading(
                col, text=heading + indicator,
                command=lambda c=col: self._sort_oa(c),
            )

    def _render_oa_rows(self) -> None:
        col = self._oa_sort_col
        asc = self._oa_sort_asc

        def sort_key(nm: NotableMove):
            if col == "sequence":
                return len(nm.move_uci_sequence)
            if col == "move":
                return nm.san
            if col == "move_wr":
                return nm.move_wr
            if col == "avg_wr":
                return nm.pos_avg_wr
            if col == "delta":
                return nm.delta
            if col == "popularity":
                return nm.popularity_rank
            return 0

        sorted_results = sorted(self._analysis_results, key=sort_key, reverse=not asc)
        self._oa_update_headings()

        # Preserve selection by iid (index into _analysis_results)
        sel = self._oa_tree.selection()

        for row in self._oa_tree.get_children():
            self._oa_tree.delete(row)

        for nm in sorted_results:
            idx = self._analysis_results.index(nm)
            self._oa_tree.insert("", "end", iid=str(idx), values=(
                self._oa_seq_str(nm),
                nm.san,
                f"{nm.move_wr:.1f}%",
                f"{nm.pos_avg_wr:.1f}%",
                f"+{nm.delta:.1f}",
                f"#{nm.popularity_rank}",
            ))

        # Restore selection if still present
        for iid in sel:
            if self._oa_tree.exists(iid):
                self._oa_tree.selection_set(iid)

    def _on_oa_complete(self, total: int) -> None:
        self._oa_run_btn.configure(state="normal")
        self._oa_stop_btn.configure(state="disabled")
        found = len(self._analysis_results)
        self._oa_status_var.set(
            f"Done. {total} positions checked, "
            f"{found} notable move{'s' if found != 1 else ''} found."
        )

    def _on_oa_row_select(self, event) -> None:
        if not self._board_ui:
            return
        sel = self._oa_tree.selection()
        if not sel:
            if hasattr(self, "_oa_detail_var"):
                self._oa_detail_var.set("")
            return
        try:
            idx = int(sel[0])
        except (ValueError, IndexError):
            return
        if idx >= len(self._analysis_results):
            return
        nm = self._analysis_results[idx]
        if hasattr(self, "_oa_detail_var"):
            self._oa_detail_var.set(self._oa_explain(nm))
        self._navigate_to_notable_move(nm)

    def _navigate_to_notable_move(self, nm: NotableMove) -> None:
        """Replay the move sequence as a game, then show the notable arrow."""
        board = chess.Board()
        moves = []
        for uci in nm.move_uci_sequence:
            try:
                move = chess.Move.from_uci(uci)
                board.push(move)
                moves.append(move)
            except Exception:
                break
        # Set arrow context BEFORE load_game so the history_callback fires with it set
        self._notable_arrow_fen = chess.Board(nm.fen).fen()
        self._notable_arrow_uci = nm.uci
        self._board_ui.load_game(chess.Board(), moves)
        # _goto_ply → _history_callback → _update_history_display → show_notable_move

    # ── Game analysis (full game eval graph) ─────────────────────────────────

    def _analyze_game(self) -> None:
        if not self._board_ui:
            return
        moves = self._board_ui._game_moves
        if not moves:
            return
        if self._analyzing_game:
            return
        self._analyzing_game = True
        self._analyze_ply_idx = 0
        self._analyze_total = len(moves) + 1
        self._board_ui.navigate_to_ply(0)
        # Advance driven by on_heatmap_done callback

    def _on_position_analyzed(self) -> None:
        """Called by board_ui after each heatmap completes — drives game analysis."""
        if not self._analyzing_game:
            return
        self._analyze_ply_idx += 1
        if self._analyze_ply_idx >= self._analyze_total:
            self._analyzing_game = False
            self._draw_eval_graph()
            self._generate_game_review()
            return
        self._board_ui.navigate_to_ply(self._analyze_ply_idx)

    # ── Game Review ───────────────────────────────────────────────────────────

    def _generate_game_review(self) -> None:
        """Build per-move review entries from already-computed heatmap data."""
        if not self._board_ui:
            return
        board = self._board_ui._game_start_board.copy()
        moves = self._board_ui._game_moves
        evals = self._board_ui._ply_position_evals
        move_scores = self._board_ui._ply_move_scores
        start_white = self._board_ui._game_start_board.turn == chess.WHITE

        self._game_review_entries = []

        for ply_0, move in enumerate(moves):
            ply = ply_0 + 1  # 1-indexed ply after this move

            marker = self._classify_move(ply)
            if not marker:
                board.push(move)
                continue

            # Centipawn drop from the mover's perspective
            prev_cp = evals.get(ply_0, 0)
            after_cp = evals.get(ply, 0)
            was_white = start_white if ply_0 % 2 == 0 else not start_white
            drop = int((prev_cp - after_cp) if was_white else (after_cp - prev_cp))

            # Best alternative from heatmap scores at the pre-move position
            scores_before = move_scores.get(ply_0, {})
            best_san = ""
            best_uci = ""
            if scores_before:
                def sort_key(item, turn=board.turn):
                    v = item[1].pov(turn).score(mate_score=30000)
                    return v if v is not None else -99999
                ranked = sorted(scores_before.items(), key=sort_key, reverse=True)
                for b_uci, _ in ranked:
                    if b_uci != move.uci():
                        try:
                            best_san = board.san(chess.Move.from_uci(b_uci))
                            best_uci = b_uci
                            break
                        except Exception:
                            pass

            explanation = BoardUI._get_move_explanation(board, move)

            self._game_review_entries.append({
                "ply": ply,
                "move_num": (ply_0 // 2) + 1,
                "was_white": was_white,
                "san": board.san(move),
                "marker": marker,
                "drop": drop,
                "best_san": best_san,
                "best_uci": best_uci,
                "explanation": explanation,
            })

            board.push(move)

        if hasattr(self, "_review_btn"):
            state = "normal" if self._game_review_entries else "disabled"
            self._review_btn.configure(state=state)

    def _show_game_review_window(self) -> None:
        if not self._game_review_entries:
            messagebox.showinfo("Game Review", "Run 'Analyze Game' first.")
            return

        win = tk.Toplevel(self._root)
        win.title("Game Review")
        win.geometry("540x620")
        win.configure(bg="#2B2B2B")
        win.resizable(True, True)

        count = len(self._game_review_entries)
        tk.Label(win, text="Game Review", bg="#2B2B2B", fg="#EEEEEE",
                 font=("Segoe UI", 12, "bold")).pack(pady=(10, 2))
        tk.Label(win, text=f"{count} move{'s' if count != 1 else ''} flagged",
                 bg="#2B2B2B", fg="#9D9D9D", font=("Segoe UI", 10)).pack(pady=(0, 6))

        outer = tk.Frame(win, bg="#2B2B2B")
        outer.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        canvas = tk.Canvas(outer, bg="#2B2B2B", highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg="#2B2B2B")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        for entry in self._game_review_entries:
            self._build_review_entry(inner, entry)

    def _build_review_entry(self, parent: tk.Frame, entry: dict) -> None:
        marker_colors = {"??": "#FF6666", "?": "#FFB347", "!?": "#FFE066"}
        color = marker_colors.get(entry["marker"], "#CCCCCC")
        side_char = "W" if entry["was_white"] else "B"

        card = tk.Frame(parent, bg="#333333", relief="flat", bd=0)
        card.pack(fill="x", padx=4, pady=3)

        # Colored left border indicates severity
        tk.Frame(card, bg=color, width=4).pack(side="left", fill="y")

        body = tk.Frame(card, bg="#333333")
        body.pack(side="left", fill="both", expand=True, padx=(6, 6), pady=6)

        # Header: move notation + cp drop + nav button
        header = tk.Frame(body, bg="#333333")
        header.pack(fill="x")

        move_text = f"{side_char}{entry['move_num']}. {entry['san']}{entry['marker']}"
        tk.Label(header, text=move_text, bg="#333333", fg=color,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Label(header, text=f"  (\u2212{entry['drop']} cp)", bg="#333333", fg="#9D9D9D",
                 font=("Segoe UI", 10)).pack(side="left")

        ply = entry["ply"]
        ttk.Button(header, text="\u2192 Go", width=5,
                   command=lambda p=ply: self._board_ui.navigate_to_ply(p)
                   ).pack(side="right")

        if entry["best_san"]:
            tk.Label(body, text=f"Better: {entry['best_san']}", bg="#333333", fg="#88CC88",
                     font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", pady=(2, 0))

        if entry["explanation"]:
            tk.Label(body, text=entry["explanation"], bg="#333333", fg="#CCCCCC",
                     font=("Segoe UI", 10), anchor="nw", justify="left",
                     wraplength=460).pack(fill="x", pady=(2, 0))

    # ── Board image export ────────────────────────────────────────────────────

    def _save_board_image(self) -> None:
        try:
            from PIL import ImageGrab
        except ImportError:
            messagebox.showerror("Export Error", "Pillow is required for image export.")
            return
        canvas = self._board_ui._canvas
        x = canvas.winfo_rootx()
        y = canvas.winfo_rooty()
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG files", "*.png"), ("All files", "*.*")],
            initialfile="board.png",
        )
        if not path:
            return
        try:
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            img.save(path)
        except Exception as e:
            messagebox.showerror("Export Error", str(e))


def main() -> None:
    root = tk.Tk()
    app = ChessAnalyzerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
