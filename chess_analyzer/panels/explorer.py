"""Opening Explorer panel: Lichess move statistics for the shown position."""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

import chess

from .. import config, theme
from ..board_ui import BoardUI
from ..lichess_api import (
    RATING_CHOICES,
    SPEED_LABELS,
    SPEEDS,
    ExplorerQuery,
    ExplorerResult,
    LichessAuthError,
    LichessClient,
    LichessError,
    LichessRateLimited,
    percentages,
)
from ..ui_dispatch import UiDispatcher

log = logging.getLogger(__name__)

DEBOUNCE_MS = 150
MAX_ROWS = 12
TOKEN_HINT = "Enter a Lichess API token above to use the Opening Explorer"
COLUMNS = (
    ("move", "Move", 70, "w"),
    ("games", "Games", 110, "e"),
    ("white", "White%", 65, "center"),
    ("draw", "Draw%", 60, "center"),
    ("black", "Black%", 65, "center"),
    ("elo", "Avg Elo", 70, "center"),
)


class ExplorerPanel(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        cfg: config.AppConfig,
        client: LichessClient,
        board_ui: BoardUI,
        dispatcher: UiDispatcher,
        save_config: Callable[[], None],
        on_opening: Callable[[str], None],
    ) -> None:
        super().__init__(parent, bg=theme.PANEL, padx=6, pady=4)
        self._cfg = cfg
        self._client = client
        self._board_ui = board_ui
        self._dispatcher = dispatcher
        self._save_config = save_config
        self._on_opening = on_opening

        self._visible = False
        self._debounce_id: str | None = None
        self._pending: tuple[str, ExplorerQuery] | None = None
        self._blocked_until = 0.0
        self._shown: ExplorerResult | None = None
        self._sort_col = "games"
        self._sort_asc = False

        self._build()

    # ── Widgets ───────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self._token_var = tk.StringVar(value=self._cfg.lichess_token)
        token_row = ttk.Frame(self)
        token_row.pack(anchor="w")
        ttk.Label(token_row, text="Lichess API Token:").pack(side="left")
        entry = ttk.Entry(token_row, textvariable=self._token_var, width=30, show="•")
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda _e: self._save_token())
        ttk.Button(token_row, text="Save", command=self._save_token).pack(side="left")
        ttk.Label(
            self, text="Get a token at lichess.org/account/oauth/token", foreground=theme.TEXT_DIM
        ).pack(anchor="w", pady=(0, 6))

        self._speed_vars: dict[str, tk.BooleanVar] = {}
        speed_row = ttk.Frame(self)
        speed_row.pack(anchor="w", pady=(0, 2))
        ttk.Label(speed_row, text="Speed:", width=7).pack(side="left")
        for speed in SPEEDS:
            var = tk.BooleanVar(value=speed in self._cfg.explorer_speeds)
            self._speed_vars[speed] = var
            ttk.Checkbutton(
                speed_row, text=SPEED_LABELS[speed], variable=var, command=self._on_filter_change
            ).pack(side="left", padx=(0, 2))

        self._rating_vars: dict[int, tk.BooleanVar] = {}
        rating_row = ttk.Frame(self)
        rating_row.pack(anchor="w", pady=(0, 4))
        ttk.Label(rating_row, text="Rating:", width=7).pack(side="left")
        for rating in RATING_CHOICES:
            var = tk.BooleanVar(value=rating in self._cfg.explorer_ratings)
            self._rating_vars[rating] = var
            ttk.Checkbutton(
                rating_row, text=str(rating), variable=var, command=self._on_filter_change
            ).pack(side="left", padx=(0, 2))

        self._status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._status_var, wraplength=280, justify="left").pack(
            anchor="w", fill="x"
        )

        self._tree = ttk.Treeview(
            self,
            columns=tuple(c[0] for c in COLUMNS),
            show="headings",
            height=6,
            style="Explorer.Treeview",
        )
        for col, heading, width, anchor in COLUMNS:
            self._tree.heading(col, text=heading, command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=width, anchor=anchor, stretch=False)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.pack(side="left", fill="x")
        scroll.pack(side="left", fill="y")

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def query(self) -> ExplorerQuery:
        return ExplorerQuery(
            speeds=tuple(s for s, v in self._speed_vars.items() if v.get()),
            ratings=tuple(r for r, v in self._rating_vars.items() if v.get()),
            moves=MAX_ROWS,
        )

    def set_visible(self, visible: bool) -> None:
        self._visible = visible
        if visible:
            self.refresh()

    def on_navigate(self) -> None:
        """The board moved to another position: refresh soon (debounced)."""
        if not self._visible:
            return
        if self._debounce_id is not None:
            self.after_cancel(self._debounce_id)
        self._debounce_id = self.after(DEBOUNCE_MS, self.refresh)

    def refresh(self) -> None:
        self._debounce_id = None
        if not self._visible:
            return
        if not self._cfg.lichess_token.strip():
            self._show_message(TOKEN_HINT)
            return
        board = self._board_ui.board
        query = self.query
        cached = self._client.cached(board, query)
        if cached is not None:
            self._populate(cached)
            return
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            self._show_message(f"Rate limited by Lichess — retrying in {remaining:.0f} s")
            self.after(int(min(remaining, 5) * 1000) + 50, self.refresh)
            return
        key = (board.epd(), query)
        if self._pending == key:
            return
        self._pending = key
        self._show_message("Loading…")
        threading.Thread(
            target=self._fetch, args=(board, query, key), daemon=True, name="explorer-fetch"
        ).start()

    # ── Fetching ──────────────────────────────────────────────────────────────

    def _fetch(self, board: chess.Board, query: ExplorerQuery, key: tuple) -> None:
        try:
            result = self._client.explorer(board, query)
        except LichessError as exc:
            self._dispatcher.post(self._on_fetch_error, key, exc)
            return
        self._dispatcher.post(self._on_fetch_done, key, result)

    def _on_fetch_done(self, key: tuple, result: ExplorerResult) -> None:
        if self._pending == key:
            self._pending = None
        if self._current_key() == key:
            self._populate(result)

    def _on_fetch_error(self, key: tuple, exc: LichessError) -> None:
        if self._pending == key:
            self._pending = None
        if isinstance(exc, LichessRateLimited):
            self._blocked_until = time.monotonic() + exc.retry_after
        if self._current_key() != key:
            return
        if isinstance(exc, LichessAuthError):
            self._show_message("Lichess rejected the token — check it and click Save")
        elif isinstance(exc, LichessRateLimited):
            self.refresh()  # shows the countdown and retries
        else:
            self._show_message(f"{exc} — will retry when the position changes")

    def _current_key(self) -> tuple[str, ExplorerQuery]:
        return (self._board_ui.board.epd(), self.query)

    # ── Display ───────────────────────────────────────────────────────────────

    def _show_message(self, text: str) -> None:
        self._status_var.set(text)
        self._shown = None
        self._clear_rows()

    def _populate(self, result: ExplorerResult) -> None:
        self._shown = result
        self._on_opening(result.opening)
        if result.total > 0:
            w, d, b = percentages(result.white, result.draws, result.black)
            prefix = f"{result.opening}   " if result.opening else ""
            self._status_var.set(
                f"{prefix}{result.total:,} games  White {w}%  Draw {d}%  Black {b}%"
            )
        else:
            self._status_var.set("No games found for these filters")
        self._render_rows()
        self._board_ui.set_explorer_move_order([m.uci for m in result.moves])

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = col == "move"
        self._render_rows()

    def _render_rows(self) -> None:
        col, asc = self._sort_col, self._sort_asc
        for c, heading, _w, _a in COLUMNS:
            indicator = (" ▲" if asc else " ▼") if c == col else ""
            self._tree.heading(c, text=heading + indicator)
        self._clear_rows()
        if self._shown is None:
            return

        def key(m):
            total = m.total or 1
            return {
                "move": m.san,
                "games": m.total,
                "white": m.white / total,
                "draw": m.draws / total,
                "black": m.black / total,
                "elo": m.average_rating or 0,
            }[col]

        for m in sorted(self._shown.moves, key=key, reverse=not asc):
            w, d, b = percentages(m.white, m.draws, m.black)
            self._tree.insert(
                "",
                "end",
                values=(
                    m.san,
                    f"{m.total:,}",
                    f"{w}%" if m.total else "-",
                    f"{d}%" if m.total else "-",
                    f"{b}%" if m.total else "-",
                    m.average_rating if m.average_rating is not None else "-",
                ),
            )

    def _clear_rows(self) -> None:
        for row in self._tree.get_children():
            self._tree.delete(row)

    # ── Interaction ───────────────────────────────────────────────────────────

    def _selected_move(self, item: str) -> chess.Move | None:
        san = self._tree.item(item, "values")[0]
        try:
            return self._board_ui.board.parse_san(san)
        except ValueError:
            return None

    def _on_select(self, _event: tk.Event) -> None:
        selection = self._tree.selection()
        move = self._selected_move(selection[0]) if selection else None
        self._board_ui.select_square(move.from_square if move else None)

    def _on_double_click(self, event: tk.Event) -> None:
        item = self._tree.identify_row(event.y)
        move = self._selected_move(item) if item else None
        if move is not None:
            self._board_ui.play_move(move)

    def _save_token(self) -> None:
        self._cfg.lichess_token = self._token_var.get().strip()
        self._save_config()
        self._client.clear_cache()
        self._pending = None
        self._blocked_until = 0.0
        self.refresh()

    def _on_filter_change(self) -> None:
        self._cfg.explorer_speeds = [s for s, v in self._speed_vars.items() if v.get()]
        self._cfg.explorer_ratings = [r for r, v in self._rating_vars.items() if v.get()]
        self._save_config()
        self.refresh()
