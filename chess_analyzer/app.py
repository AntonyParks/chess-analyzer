"""The application window: wiring between the board, the engine, the panels and settings."""

from __future__ import annotations

import logging
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import chess
import chess.pgn

from . import __version__, board_image, config, logging_setup, paths, sound, theme
from .board_ui import BoardUI
from .engine_setup import ensure_engine
from .engine_worker import EngineWorker
from .game_state import GameState
from .lichess_api import LichessClient
from .opening_insights import InsightsWorker
from .panels.engine_lines import EngineLinesPanel
from .panels.explorer import ExplorerPanel
from .panels.history import EvalGraph, HistoryPanel
from .panels.insights import InsightsWindow
from .panels.review import GameAnalysisRunner, ReviewEntry, ReviewWindow, build_review
from .pieces import PieceImages
from .ui_dispatch import UiDispatcher

log = logging.getLogger(__name__)

MIN_WINDOW = (920, 700)
RESIZE_DEBOUNCE_MS = 80
LAYOUT_SLACK_PX = 24  # room around the board when sizing it to the window height
PGN_FILETYPES = [("PGN files", "*.pgn"), ("All files", "*.*")]


class ChessAnalyzerApp:
    def __init__(
        self, root: tk.Tk, cfg: config.AppConfig, dispatcher: UiDispatcher, data_dir: Path
    ) -> None:
        self._root = root
        self._cfg = cfg
        self._dispatcher = dispatcher
        self._data_dir = data_dir
        self._config_save_failed = False
        self._pieces = PieceImages()

        self._engine_worker: EngineWorker | None = None
        self._board_ui: BoardUI | None = None
        self._runner: GameAnalysisRunner | None = None
        self._insights: InsightsWindow | None = None
        self._review_entries: list[ReviewEntry] = []
        self._pgn_path = ""
        self._last_sound_ply = 0
        self._opening_text = ""
        self._resize_after_id: str | None = None

        self._depth_var = tk.IntVar(value=cfg.depth)
        self._heatmap_var = tk.BooleanVar(value=cfg.heatmap)
        self._arrow_var = tk.BooleanVar(value=cfg.show_arrow)
        self._flip_var = tk.BooleanVar(value=cfg.flip)
        self._explorer_var = tk.BooleanVar(value=False)

        # Lichess data: one client shared by the explorer panel and Opening Insights
        self._lichess = LichessClient(lambda: self._cfg.lichess_token)
        self._insights_worker = InsightsWorker(self._lichess, self._dispatcher)

        self._configure_window()
        self._apply_styles()
        self._setup_engine_then_launch()

    # ── Window ────────────────────────────────────────────────────────────────

    def _configure_window(self) -> None:
        self._root.title("Chess Analysis Tool")
        self._root.minsize(*MIN_WINDOW)
        self._root.configure(bg=theme.BG)
        if self._cfg.window_geometry:
            self._root.geometry(self._cfg.window_geometry)
        else:
            self._root.after(120, self._maximize)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _maximize(self) -> None:
        try:
            self._root.state("zoomed")
        except tk.TclError:
            try:
                self._root.attributes("-zoomed", True)
            except tk.TclError:
                log.debug("Window manager does not support maximising", exc_info=True)

    def _apply_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            ".",
            background=theme.PANEL,
            foreground=theme.TEXT,
            fieldbackground=theme.FIELD,
            font=theme.ui(10),
        )
        style.configure("TLabel", background=theme.PANEL, foreground=theme.TEXT, font=theme.ui(10))
        style.configure(
            "TButton", background=theme.BUTTON, foreground=theme.TEXT_BRIGHT, font=theme.ui(10)
        )
        style.map("TButton", background=[("active", theme.BUTTON_ACTIVE)])
        style.configure("TLabelframe", background=theme.PANEL, foreground=theme.TEXT)
        style.configure(
            "TLabelframe.Label",
            background=theme.PANEL,
            foreground=theme.TEXT,
            font=theme.ui(10, "bold"),
        )
        style.configure(
            "TEntry", fieldbackground=theme.FIELD, foreground=theme.TEXT_BRIGHT, font=theme.ui(10)
        )
        style.configure(
            "TSpinbox", fieldbackground=theme.FIELD, foreground=theme.TEXT_BRIGHT, font=theme.ui(10)
        )
        style.configure(
            "Explorer.Treeview",
            background=theme.TABLE_BG,
            foreground=theme.TABLE_FG,
            fieldbackground=theme.TABLE_BG,
            rowheight=22,
        )
        style.configure(
            "Explorer.Treeview.Heading",
            background=theme.TABLE_HEADING,
            foreground=theme.TABLE_FG,
            font=theme.ui(9, "bold"),
        )
        style.map(
            "Explorer.Treeview",
            background=[("selected", theme.SELECTED)],
            foreground=[("selected", theme.WHITE)],
        )

    # ── Startup: find or download the engine, then build the UI ───────────────

    def _setup_engine_then_launch(self) -> None:
        splash = tk.Toplevel(self._root)
        splash.title("Setting up...")
        splash.geometry("420x110")
        splash.resizable(False, False)
        splash.configure(bg=theme.PANEL)
        splash.grab_set()
        label = tk.Label(
            splash,
            text="Checking for the chess engine...",
            bg=theme.PANEL,
            fg=theme.TEXT_LABEL,
            font=theme.mono(10),
        )
        label.pack(pady=(18, 8))
        bar = ttk.Progressbar(splash, mode="indeterminate", length=360)
        bar.pack(pady=(0, 12))
        bar.start(12)

        def show_progress(message: str, fraction: float | None) -> None:
            label.configure(text=message)
            if fraction is None:
                if str(bar["mode"]) != "indeterminate":
                    bar.configure(mode="indeterminate")
                    bar.start(12)
            else:
                if str(bar["mode"]) != "determinate":
                    bar.stop()
                    bar.configure(mode="determinate", maximum=1.0)
                bar.configure(value=fraction)

        def report(message: str, fraction: float | None = None) -> None:
            self._dispatcher.post(show_progress, message, fraction)

        def do_setup() -> None:
            try:
                exe_path = ensure_engine(
                    report,
                    engines_dir=paths.engines_dir(self._data_dir),
                    config_path=self._cfg.engine_path,
                )
            except Exception as exc:
                log.exception("Engine setup failed")
                self._dispatcher.post(self._on_setup_failure, exc, splash)
                return
            self._dispatcher.post(self._finish_launch, exe_path, splash)

        threading.Thread(target=do_setup, daemon=True, name="engine-setup").start()

    def _on_setup_failure(self, error: Exception, splash: tk.Toplevel) -> None:
        splash.destroy()
        messagebox.showerror(
            "Engine Error",
            f"Could not start Stockfish:\n{error}\n\n"
            f"Place a Stockfish binary in {paths.engines_dir(self._data_dir)}\n"
            f"or point the {paths.ENV_ENGINE_PATH} environment variable at one.",
        )
        self._on_close()

    def _finish_launch(self, exe_path: Path, splash: tk.Toplevel) -> None:
        splash.destroy()
        self._engine_worker = EngineWorker(
            exe_path,
            self._dispatcher,
            threads=self._cfg.engine_threads,
            hash_mb=self._cfg.engine_hash_mb,
            on_error=self._on_engine_error,
        )

        # Bottom status bar first so it keeps its height; then [controls | board | history]
        self._status_frame = tk.Frame(self._root, bg=theme.STATUS_BG, pady=3)
        self._status_frame.pack(side="bottom", fill="x", padx=4)
        top = tk.Frame(self._root, bg=theme.BG)
        top.pack(side="top", fill="both", expand=True)

        self._controls_frame = tk.Frame(top, bg=theme.PANEL, padx=4, pady=4)
        self._controls_frame.pack(side="left", fill="y", padx=(4, 0), pady=4)
        self._build_load_panel(self._controls_frame)
        self._build_toggle_panel(self._controls_frame)

        board_frame = tk.Frame(top, bg=theme.BG)
        board_frame.pack(side="left", fill="both", expand=True)
        self._board_ui = BoardUI(
            board_frame,
            worker=self._engine_worker,
            state=GameState(),
            pieces=self._pieces,
            depth=self._depth_var.get(),
            on_navigate=self._on_board_navigate,
            on_evals_updated=self._on_evals_updated,
        )
        self._board_ui.pack(padx=4, pady=4)
        self._board_ui.set_heatmap(self._heatmap_var.get())
        self._board_ui.set_show_arrow(self._arrow_var.get())
        self._board_ui.set_flipped(self._flip_var.get())
        self._eval_graph = EvalGraph(board_frame, board_ui=self._board_ui)
        self._eval_graph.pack(side="bottom", fill="x", padx=4, pady=(2, 4))

        self._history = HistoryPanel(top, board_ui=self._board_ui)
        self._history.pack(side="left", fill="y", padx=(0, 4), pady=4)

        self._build_status_bar()

        self._explorer_panel = ExplorerPanel(
            self._controls_frame,
            cfg=self._cfg,
            client=self._lichess,
            board_ui=self._board_ui,
            dispatcher=self._dispatcher,
            save_config=self._save_config,
            on_opening=self._set_opening,
        )  # packed when the Opening Explorer checkbox is enabled
        self._engine_lines = EngineLinesPanel(self._controls_frame, board_ui=self._board_ui)
        self._engine_lines.pack(fill="x", padx=6, pady=4)

        self._runner = GameAnalysisRunner(
            worker=self._engine_worker,
            board_ui=self._board_ui,
            depth_provider=self._depth_var.get,
            on_progress=self._on_analysis_progress,
            on_finished=self._on_analysis_finished,
        )

        nav_keys = (("<Left>", "prev"), ("<Right>", "next"), ("<Home>", "first"), ("<End>", "last"))
        for key, action in nav_keys:
            self._root.bind(key, lambda e, a=action: self._on_nav_key(e, a))
        self._root.bind("<Configure>", self._on_window_configure)

        self._on_board_navigate(0)

    # ── Panels built inline ───────────────────────────────────────────────────

    def _build_load_panel(self, parent: tk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Load Position", padding=6)
        frame.pack(fill="x", padx=6, pady=4)

        fen_row = ttk.Frame(frame)
        fen_row.pack(fill="x", pady=2)
        ttk.Label(fen_row, text="FEN:").pack(side="left")
        self._fen_var = tk.StringVar()
        entry = ttk.Entry(fen_row, textvariable=self._fen_var, width=44)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda _e: self._load_fen())
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

        row1 = ttk.Frame(frame)
        row1.pack(fill="x", pady=(2, 1))
        ttk.Button(row1, text="Starting Position", command=self._load_start).pack(side="left")
        ttk.Button(row1, text="Undo Move", command=self._undo_move).pack(side="left", padx=4)
        ttk.Button(row1, text="Copy FEN", command=self._copy_fen).pack(side="left")
        ttk.Button(row1, text="Save PGN", command=self._save_pgn).pack(side="left", padx=4)

        row2 = ttk.Frame(frame)
        row2.pack(fill="x", pady=(1, 2))
        ttk.Button(row2, text="Save Image", command=self._save_board_image).pack(side="left")
        self._analyze_btn = ttk.Button(row2, text="Analyze Game", command=self._analyze_game)
        self._analyze_btn.pack(side="left", padx=4)
        self._review_btn = ttk.Button(
            row2, text="Review Game", command=self._show_review, state="disabled"
        )
        self._review_btn.pack(side="left", padx=(0, 4))
        ttk.Label(row2, text="Depth:").pack(side="left")
        spin = ttk.Spinbox(
            row2,
            from_=config.MIN_DEPTH,
            to=config.MAX_DEPTH,
            textvariable=self._depth_var,
            width=4,
            command=self._on_depth_change,
        )
        spin.pack(side="left", padx=(2, 0))
        spin.bind("<Return>", lambda _e: self._on_depth_change())

    def _build_toggle_panel(self, parent: tk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Eval Display", padding=6)
        frame.pack(fill="x", padx=6, pady=4)
        for text, var, command in (
            ("Piece Heatmap", self._heatmap_var, self._on_heatmap_change),
            ("Best Move Arrow", self._arrow_var, self._on_arrow_change),
            ("⇅ Flip Board", self._flip_var, self._on_flip_change),
        ):
            ttk.Checkbutton(frame, text=text, variable=var, command=command).pack(
                anchor="w", pady=1
            )
        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=4)
        ttk.Checkbutton(
            frame,
            text="Opening Explorer",
            variable=self._explorer_var,
            command=self._on_explorer_toggle,
        ).pack(anchor="w", pady=1)
        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=4)
        ttk.Button(frame, text="Opening Insights...", command=self._open_insights).pack(
            anchor="w", pady=1
        )

    def _build_status_bar(self) -> None:
        self._turn_var = tk.StringVar(value="")
        self._state_var = tk.StringVar(value="")
        self._move_var = tk.StringVar(value="")
        self._opening_var = tk.StringVar(value="")
        self._analysis_var = tk.StringVar(value="")

        def label(var: tk.StringVar, fg: str, font: tuple, **pack: object) -> tk.Label:
            widget = tk.Label(
                self._status_frame, textvariable=var, bg=theme.STATUS_BG, fg=fg, font=font
            )
            widget.pack(side="left", **pack)
            return widget

        label(self._turn_var, theme.TEXT_BRIGHT, theme.ui(10), padx=(8, 0))
        self._state_label = label(self._state_var, theme.WARN, theme.ui(10, "bold"), padx=4)
        label(self._move_var, theme.TEXT_DIM, theme.ui(10), padx=8)
        label(self._opening_var, theme.TEXT_MUTED, theme.ui(10, "italic"), padx=4)
        label(self._analysis_var, theme.GOOD, theme.ui(10), padx=8)

    # ── Board callbacks ───────────────────────────────────────────────────────

    def _on_board_navigate(self, ply: int) -> None:
        """The shown position changed (navigation, a new move, or a new game)."""
        self._runner.on_state_changed()
        self._engine_lines.clear_selection()
        self._refresh_views()
        self._explorer_panel.on_navigate()
        if not self._runner.running:
            self._engine_lines.refresh()

    def _on_evals_updated(self, ply: int, complete: bool) -> None:
        """Engine results for the shown position arrived (partial or complete)."""
        self._refresh_views()
        if complete and not self._runner.running:
            self._engine_lines.refresh()

    def _refresh_views(self) -> None:
        self._update_status_bar()
        ply = self._board_ui.state.current_ply
        if ply > self._last_sound_ply:
            sound.play_move()
        self._last_sound_ply = ply
        self._history.refresh()
        self._eval_graph.redraw()

    def _on_nav_key(self, event: tk.Event, action: str) -> str | None:
        if isinstance(event.widget, (tk.Entry, tk.Spinbox, tk.Text)):
            return None  # let the text widget use the arrow keys
        getattr(self._board_ui, action)()
        return "break"

    def _update_status_bar(self) -> None:
        board = self._board_ui.board
        self._turn_var.set("White to move" if board.turn == chess.WHITE else "Black to move")
        self._move_var.set(
            f"Move {board.fullmove_number}  ·  Ply {self._board_ui.state.current_ply}"
        )
        outcome = board.outcome(claim_draw=True)
        if outcome is not None and outcome.termination == chess.Termination.CHECKMATE:
            state, colour = "✕ Checkmate", theme.BAD
        elif outcome is not None:
            state = "½ " + outcome.termination.name.replace("_", " ").capitalize()
            colour = theme.TEXT_MUTED
        elif board.is_check():
            state, colour = "⚑ Check!", theme.WARN
        else:
            state, colour = "", theme.WARN
        self._state_var.set(state)
        self._state_label.configure(fg=colour)
        self._opening_var.set(self._opening_text)

    def _set_opening(self, text: str) -> None:
        """The explorer learned the opening name of the shown position."""
        self._opening_text = text
        self._opening_var.set(text)

    # ── Loading positions and games ───────────────────────────────────────────

    def _load_fen(self) -> None:
        fen = self._fen_var.get().strip()
        if not fen:
            messagebox.showwarning("Empty FEN", "Paste a FEN string first.")
            return
        try:
            board = chess.Board(fen)
        except ValueError as exc:
            messagebox.showerror("Invalid FEN", f"Could not parse FEN:\n{exc}")
            return
        if not board.is_valid():
            messagebox.showerror(
                "Invalid position", f"That position is not legal ({board.status()!r})."
            )
            return
        self._load_new_game(board, [], {})

    def _browse_pgn(self) -> None:
        path = filedialog.askopenfilename(
            title="Open PGN File",
            initialdir=self._cfg.last_pgn_dir or None,
            filetypes=PGN_FILETYPES,
        )
        if path:
            self._pgn_path = path
            self._pgn_label_var.set(Path(path).name)
            self._cfg.last_pgn_dir = str(Path(path).parent)
            self._save_config()

    def _load_pgn(self) -> None:
        if not self._pgn_path:
            messagebox.showwarning("No File", "Browse for a PGN file first.")
            return
        try:
            with open(self._pgn_path, encoding="utf-8", errors="replace") as f:
                game = chess.pgn.read_game(f)
                more_games = game is not None and chess.pgn.read_game(f) is not None
        except (OSError, ValueError) as exc:
            messagebox.showerror("PGN Error", str(exc))
            return
        if game is None:
            messagebox.showerror("PGN Error", "No game found in the file.")
            return
        loaded = GameState.from_pgn(game)
        self._load_new_game(loaded.start_board, loaded.moves, loaded.headers)
        if game.errors:
            messagebox.showwarning(
                "PGN warnings",
                f"{len(game.errors)} problem(s) were found while reading the game; "
                f"{len(loaded.moves)} moves were loaded.\n\nFirst problem: {game.errors[0]}",
            )
        if more_games:
            self._pgn_label_var.set(f"{Path(self._pgn_path).name} (first game of several)")

    def _load_start(self) -> None:
        self._load_new_game(chess.Board(), [], {})

    def _load_new_game(
        self, start_board: chess.Board, moves: list[chess.Move], headers: dict[str, str]
    ) -> None:
        self._runner.cancel()
        self._review_entries = []
        self._review_btn.configure(state="disabled")
        self._set_opening("")
        self._board_ui.load_game(start_board, moves, headers)

    def _undo_move(self) -> None:
        self._board_ui.undo()

    def _copy_fen(self) -> None:
        self._root.clipboard_clear()
        self._root.clipboard_append(self._board_ui.get_fen())

    def _save_pgn(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".pgn",
            initialdir=self._cfg.last_pgn_dir or None,
            filetypes=PGN_FILETYPES,
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._board_ui.state.to_pgn())
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))

    def _save_board_image(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            initialdir=self._cfg.last_pgn_dir or None,
            filetypes=[("PNG files", "*.png"), ("All files", "*.*")],
            initialfile="board.png",
        )
        if not path:
            return
        ui = self._board_ui
        try:
            image = board_image.render(
                ui.board,
                pieces=self._pieces,
                square=ui.square_size,
                flipped=ui.flipped,
                last_move=ui.state.last_move(),
                position_cp=ui.position_cp,
                best_move=ui.best_move if self._arrow_var.get() else None,
                heatmap=ui.heatmap_data if self._heatmap_var.get() else None,
                rank_map=ui.rank_map,
            )
            image.save(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Export Error", str(exc))

    # ── Game analysis and review ──────────────────────────────────────────────

    def _analyze_game(self) -> None:
        if self._runner.running:
            return
        if not self._board_ui.state.moves:
            messagebox.showinfo("Analyze Game", "Load a PGN or play some moves first.")
            return
        self._review_entries = []
        self._review_btn.configure(state="disabled")
        self._analyze_btn.configure(state="disabled")
        self._runner.start()

    def _on_analysis_progress(self, done: int, total: int) -> None:
        self._analysis_var.set(f"Analysing game… {done}/{total}" if done < total else "")
        self._history.refresh()
        self._eval_graph.redraw()

    def _on_analysis_finished(self, completed: bool) -> None:
        self._analysis_var.set("")
        self._analyze_btn.configure(state="normal")
        self._history.refresh()
        self._eval_graph.redraw()
        self._engine_lines.refresh()
        if not completed:
            return
        self._review_entries = build_review(self._board_ui.state)
        self._review_btn.configure(state="normal" if self._review_entries else "disabled")
        if not self._review_entries:
            self._analysis_var.set("Game analysed: no inaccuracies found")

    def _show_review(self) -> None:
        if not self._review_entries:
            messagebox.showinfo("Game Review", "Run 'Analyze Game' first.")
            return
        ReviewWindow(self._root, self._review_entries, on_goto=self._board_ui.goto)

    # ── Settings and toggles ──────────────────────────────────────────────────

    def _on_depth_change(self) -> None:
        try:
            depth = int(self._depth_var.get())
        except (ValueError, tk.TclError):
            return
        depth = max(config.MIN_DEPTH, min(config.MAX_DEPTH, depth))
        self._depth_var.set(depth)
        self._board_ui.set_depth(depth)
        self._cfg.depth = depth
        self._save_config()

    def _on_heatmap_change(self) -> None:
        self._board_ui.set_heatmap(self._heatmap_var.get())
        self._cfg.heatmap = self._heatmap_var.get()
        self._save_config()

    def _on_arrow_change(self) -> None:
        self._board_ui.set_show_arrow(self._arrow_var.get())
        self._cfg.show_arrow = self._arrow_var.get()
        self._save_config()

    def _on_flip_change(self) -> None:
        self._board_ui.set_flipped(self._flip_var.get())
        self._cfg.flip = self._flip_var.get()
        self._save_config()

    def _on_explorer_toggle(self) -> None:
        if self._explorer_var.get():
            self._explorer_panel.pack(fill="x", pady=(4, 0), before=self._engine_lines)
            self._explorer_panel.set_visible(True)
        else:
            self._explorer_panel.set_visible(False)
            self._explorer_panel.pack_forget()

    def _open_insights(self) -> None:
        if self._insights is not None:
            self._insights.lift()
            return
        self._insights = InsightsWindow(
            self._root,
            cfg=self._cfg,
            board_ui=self._board_ui,
            worker=self._insights_worker,
            on_close=self._on_insights_closed,
        )

    def _on_insights_closed(self) -> None:
        self._insights = None

    def _on_engine_error(self, message: str) -> None:
        """The engine died and could not be restarted; analysis is off for this session."""
        self._state_var.set("⚠ Engine unavailable")
        messagebox.showerror("Engine error", message)

    def _save_config(self) -> None:
        """Persist settings; warn once per session if the data dir is not writable."""
        try:
            config.save(self._cfg, paths.config_path(self._data_dir))
        except OSError as exc:
            log.warning("Could not save config: %s", exc)
            if not self._config_save_failed:
                self._config_save_failed = True
                messagebox.showwarning(
                    "Settings not saved",
                    f"Could not write {paths.config_path(self._data_dir)}:\n{exc}",
                )

    # ── Resize ────────────────────────────────────────────────────────────────

    def _on_window_configure(self, event: tk.Event) -> None:
        if event.widget is not self._root:
            return
        if self._resize_after_id is not None:
            self._root.after_cancel(self._resize_after_id)
        self._resize_after_id = self._root.after(RESIZE_DEBOUNCE_MS, self._apply_resize)

    def _apply_resize(self) -> None:
        self._resize_after_id = None
        try:
            available = (
                self._root.winfo_height()
                - self._status_frame.winfo_height()
                - self._eval_graph.winfo_height()
                - LAYOUT_SLACK_PX
            )
        except tk.TclError:  # window being torn down
            return
        self._board_ui.resize(available // 8)
        self._eval_graph.redraw()

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        try:
            zoomed = self._root.state() == "zoomed"
            self._cfg.window_geometry = "" if zoomed else self._root.geometry()
        except tk.TclError:
            pass
        self._save_config()
        self._insights_worker.stop()
        if self._engine_worker is not None:
            self._engine_worker.shutdown()
        self._dispatcher.close()
        self._root.destroy()


def _enable_dpi_awareness() -> None:
    """Ask Windows for crisp (non-blurry) rendering; harmless elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:  # very old Windows, or already set by the host process
        log.debug("Could not enable DPI awareness", exc_info=True)


def main() -> None:
    data_dir = paths.user_data_dir()
    log_path = logging_setup.configure(paths.logs_dir(data_dir))
    log.info("Chess Analyzer %s starting (data dir: %s, log: %s)", __version__, data_dir, log_path)
    paths.migrate_legacy_files(Path(__file__).resolve().parent, data_dir)
    cfg = config.load(paths.config_path(data_dir))
    if cfg.dpi_aware:
        _enable_dpi_awareness()

    root = tk.Tk()
    theme.init(root)
    dispatcher = UiDispatcher(root)
    dispatcher.start()
    ChessAnalyzerApp(root, cfg, dispatcher, data_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
