"""
main.py
Chess Analysis Tool — entry point.
"""

import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
import threading
import urllib.parse
import urllib.request

import chess
import chess.pgn

from stockfish_setup import StockfishSetup
from engine_worker import EngineWorker
from board_ui import BoardUI, _score_to_color


class ChessAnalyzerApp:
    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._root.title("Chess Analysis Tool")
        self._root.resizable(False, False)
        self._root.configure(bg="#2B2B2B")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#3C3F41", foreground="#BBBBBB", fieldbackground="#45494A")
        style.configure("TLabel", background="#3C3F41", foreground="#BBBBBB")
        style.configure("TButton", background="#4C5052", foreground="#BBBBBB")
        style.configure("TLabelframe", background="#3C3F41", foreground="#BBBBBB")
        style.configure("TLabelframe.Label", background="#3C3F41", foreground="#AAAAAA")
        style.configure("TEntry", fieldbackground="#45494A", foreground="#EEEEEE")
        style.configure("TSpinbox", fieldbackground="#45494A", foreground="#EEEEEE")
        style.map("TButton", background=[("active", "#5C6366")])

        self._pgn_path: str = ""
        self._depth_var = tk.IntVar(value=15)
        self._mode_var = tk.StringVar(value="number")

        self._engine_worker: EngineWorker = None  # type: ignore
        self._board_ui: BoardUI = None  # type: ignore

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

        # ── Main layout: [eval bar + board] left | [history panel] right ──────
        top_frame = tk.Frame(self._root, bg="#2B2B2B")
        top_frame.pack(side="top", fill="both")

        board_frame = tk.Frame(top_frame, bg="#2B2B2B")
        board_frame.pack(side="left", fill="both")

        # History panel (right of board)
        history_frame = tk.Frame(top_frame, bg="#3C3F41", padx=4, pady=4)
        history_frame.pack(side="left", fill="y", padx=(0, 4), pady=4)

        ttk.Label(history_frame, text="Move History").pack()

        # Scrollable button grid — avoids all tk.Text tag rendering/click issues on Windows
        scroll_container = tk.Frame(history_frame, bg="#2B2B2B")
        scroll_container.pack(fill="both", expand=True)

        self._hist_canvas = tk.Canvas(
            scroll_container, bg="#2B2B2B", highlightthickness=0, width=190,
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
        self._history_btn_data: list = []   # [(button, ply_0based, uci), ...]
        self._history_snapshot: tuple = (-1, -1)  # (move_count, current_ply) at last rebuild

        nav_frame = tk.Frame(history_frame, bg="#3C3F41")
        nav_frame.pack(fill="x", pady=(4, 0))
        for text, cmd in [
            ("⏮", lambda: self._board_ui.navigate_first()),
            ("◀", lambda: self._board_ui.navigate_prev()),
            ("▶", lambda: self._board_ui.navigate_next()),
            ("⏭", lambda: self._board_ui.navigate_last()),
        ]:
            tk.Button(
                nav_frame, text=text, width=3,
                bg="#4C5052", fg="#EEEEEE",
                activebackground="#5C6366",
                command=cmd,
            ).pack(side="left", padx=2)

        self._board_ui = BoardUI(
            parent=board_frame,
            engine_worker=self._engine_worker,
            initial_board=chess.Board(),
            display_mode="number",
            history_callback=self._update_history_display,
        )
        self._board_ui.set_heatmap_mode(True)
        self._board_ui.set_show_arrow(False)

        # Controls below
        controls_frame = tk.Frame(self._root, bg="#3C3F41", pady=4)
        controls_frame.pack(side="top", fill="x", padx=4)

        self._build_load_panel(controls_frame)
        self._build_toggle_panel(controls_frame)

        # Opening Explorer panel (hidden until toggled on)
        self._explorer_cache: dict = {}
        self._explorer_fetch_pending: str = ""
        self._explorer_frame = tk.Frame(self._root, bg="#3C3F41", padx=6, pady=4)
        # Not packed yet — appears when checkbox is enabled

        # Token input row
        config = self._load_config()
        self._token_var = tk.StringVar(value=config.get("lichess_token", ""))
        token_row = ttk.Frame(self._explorer_frame)
        token_row.pack(anchor="w", pady=(0, 4))
        ttk.Label(token_row, text="Lichess API Token:").pack(side="left")
        ttk.Entry(token_row, textvariable=self._token_var, width=42).pack(side="left", padx=4)
        ttk.Button(token_row, text="Save", command=self._save_token).pack(side="left")
        ttk.Label(
            token_row, text="  Get token at lichess.org/account/oauth/token",
            foreground="#888888",
        ).pack(side="left", padx=4)

        self._explorer_status_var = tk.StringVar(value="")
        ttk.Label(
            self._explorer_frame,
            textvariable=self._explorer_status_var,
        ).pack(anchor="w")

        cols = ("move", "games", "white", "draw", "black", "elo")
        self._explorer_tree = ttk.Treeview(
            self._explorer_frame, columns=cols, show="headings", height=6,
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
        self._explorer_tree.pack(side="left", fill="x")
        explorer_scroll.pack(side="left", fill="y")

        # Arrow key navigation
        self._root.bind("<Left>",  lambda _: self._board_ui.navigate_prev())
        self._root.bind("<Right>", lambda _: self._board_ui.navigate_next())
        self._root.bind("<Home>",  lambda _: self._board_ui.navigate_first())
        self._root.bind("<End>",   lambda _: self._board_ui.navigate_last())

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── History display ───────────────────────────────────────────────────────

    def _update_history_display(self) -> None:
        if self._board_ui is None:
            return
        moves = self._board_ui._game_moves
        current_ply = self._board_ui._current_ply
        start_board = self._board_ui._game_start_board
        ply_rank_maps = self._board_ui._ply_rank_maps

        snapshot = (len(moves), current_ply)

        if snapshot == self._history_snapshot and self._history_btn_data:
            # Structure unchanged — just recolor buttons in-place (no flash)
            for btn, ply, uci in self._history_btn_data:
                is_current = (ply + 1 == current_ply)
                t_val = ply_rank_maps.get(ply, {}).get(uci)
                move_color = (
                    ("#FFD700" if t_val >= 1.0 else _score_to_color(t_val))
                    if t_val is not None else "#AAAAAA"
                )
                btn.configure(
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

            if is_white:
                tk.Label(
                    self._history_inner,
                    text=f"{move_num}.",
                    bg="#2B2B2B", fg="#666666",
                    font=("Consolas", 9),
                    anchor="e", width=3,
                ).grid(row=grid_row, column=0, sticky="ew", padx=(4, 0))
                col = 1
            else:
                col = 2

            btn = tk.Button(
                self._history_inner,
                text=san,
                bg="#3A5A8A" if is_current else "#2B2B2B",
                fg="#FFFFFF" if is_current else move_color,
                font=("Consolas", 9),
                relief="flat", bd=0, padx=4,
                cursor="hand2",
                activebackground="#4A6A9A",
                activeforeground="#FFFFFF",
                anchor="w",
                command=lambda p=ply + 1: self._board_ui.navigate_to_ply(p),
            )
            btn.grid(row=grid_row, column=col, sticky="ew", padx=1, pady=1)
            self._history_btn_data.append((btn, ply, uci))

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

    # ── Control panels ────────────────────────────────────────────────────────

    def _build_load_panel(self, parent: tk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Load Position", padding=6)
        frame.pack(fill="x", padx=6, pady=4)

        fen_row = ttk.Frame(frame)
        fen_row.pack(fill="x", pady=2)
        ttk.Label(fen_row, text="FEN:").pack(side="left")
        self._fen_var = tk.StringVar()
        ttk.Entry(fen_row, textvariable=self._fen_var, width=56).pack(side="left", padx=4)
        ttk.Button(fen_row, text="Load FEN", command=self._load_fen).pack(side="left")

        pgn_row = ttk.Frame(frame)
        pgn_row.pack(fill="x", pady=2)
        ttk.Label(pgn_row, text="PGN:").pack(side="left")
        self._pgn_label_var = tk.StringVar(value="no file loaded")
        ttk.Label(pgn_row, textvariable=self._pgn_label_var, width=36, anchor="w").pack(
            side="left", padx=4
        )
        ttk.Button(pgn_row, text="Browse...", command=self._browse_pgn).pack(side="left")
        ttk.Button(pgn_row, text="Load PGN", command=self._load_pgn).pack(side="left", padx=4)

        opt_row = ttk.Frame(frame)
        opt_row.pack(fill="x", pady=2)
        ttk.Button(opt_row, text="Starting Position", command=self._load_start).pack(side="left")
        ttk.Button(opt_row, text="Undo Move", command=self._undo_move).pack(side="left", padx=6)
        ttk.Button(opt_row, text="Copy FEN", command=self._copy_fen).pack(side="left")
        ttk.Button(opt_row, text="Save PGN", command=self._save_pgn).pack(side="left", padx=4)
        ttk.Label(opt_row, text="   Engine depth:").pack(side="left")
        spin = ttk.Spinbox(
            opt_row,
            from_=5, to=25,
            textvariable=self._depth_var,
            width=4,
            command=self._on_depth_change,
        )
        spin.pack(side="left")
        spin.bind("<Return>", lambda _: self._on_depth_change())

    def _build_toggle_panel(self, parent: tk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Eval Display", padding=6)
        frame.pack(fill="x", padx=6, pady=4)

        self._heatmap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame, text="Piece Heatmap",
            variable=self._heatmap_var,
            command=self._on_heatmap_change,
        ).pack(side="left", padx=6)

        self._arrow_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="Best Move Arrow",
            variable=self._arrow_var,
            command=self._on_arrow_change,
        ).pack(side="left", padx=6)

        self._flip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="⇅ Flip Board",
            variable=self._flip_var,
            command=self._on_flip_change,
        ).pack(side="left", padx=6)

        ttk.Separator(frame, orient="vertical").pack(side="left", fill="y", padx=10)

        self._explorer_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="Opening Explorer",
            variable=self._explorer_var,
            command=self._on_explorer_toggle,
        ).pack(side="left", padx=6)

    # ── Button handlers ───────────────────────────────────────────────────────

    def _load_fen(self) -> None:
        fen = self._fen_var.get().strip()
        if not fen:
            messagebox.showwarning("Empty FEN", "Paste a FEN string first.")
            return
        try:
            board = chess.Board(fen)
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
        except Exception as e:
            messagebox.showerror("PGN Error", str(e))

    def _load_start(self) -> None:
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
        self._explorer_fetch_pending = ""
        if self._explorer_var.get():
            self._refresh_explorer()

    def _on_explorer_toggle(self) -> None:
        if self._explorer_var.get():
            self._explorer_frame.pack(side="top", fill="x", padx=4, pady=(0, 4))
            self._refresh_explorer()
        else:
            self._explorer_frame.pack_forget()

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
        if fen in self._explorer_cache:
            self._populate_explorer(self._explorer_cache[fen], fen)
            return
        if fen == self._explorer_fetch_pending:
            return  # already fetching this position
        self._explorer_fetch_pending = fen
        self._explorer_status_var.set("Loading...")
        for row in self._explorer_tree.get_children():
            self._explorer_tree.delete(row)
        threading.Thread(
            target=self._fetch_explorer, args=(fen, token), daemon=True,
        ).start()

    def _fetch_explorer(self, fen: str, token: str) -> None:
        url = "https://explorer.lichess.ovh/lichess?" + urllib.parse.urlencode({
            "fen": fen,
            "topGames": 0,
            "recentGames": 0,
            "variant": "standard",
        })
        headers = {"User-Agent": "ChessAnalyzerApp/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            self._explorer_cache[fen] = data
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
        if total > 0:
            status = (
                f"{total:,} games  "
                f"White {100 * total_w // total}%  "
                f"Draw {100 * total_d // total}%  "
                f"Black {100 * total_b // total}%"
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


def main() -> None:
    root = tk.Tk()
    app = ChessAnalyzerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
