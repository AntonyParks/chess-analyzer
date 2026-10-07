# Changelog

## 1.1.0 — 2026-10-07

The user interface is rebuilt in Qt (PySide6); everything below the UI is unchanged.

### Added
- Analysis-board layout: the board fills the window and scales with it; a right column with
  **Moves**, **Explorer**, **Insights** and **Review** tabs plus **Engine lines**; a resizable
  splitter whose position is remembered.
- Menu bar, toolbar and keyboard shortcuts for everything (open/save/recent PGNs, FEN
  load/copy/paste, navigation, flip/heatmap/arrow, analyse, tabs, settings).
- **Settings** dialog for the Lichess token (masked) and engine options.
- The Explorer table shows Stockfish's evaluation of each move (an **Eval** column, coloured
  like the heatmap, sortable), filled in from the analysis of the shown position.
- The Explorer table remembers its sort separately for White-to-move and Black-to-move
  positions (e.g. White% for your moves, Games for the replies) and switches as you step
  through the game; both are kept across sessions.
- The Insights table shows how often each listed move is played from its position (a
  **Played** column with the share of games — sortable, raw counts in the tooltip), so rare
  sidelines are easy to skip.
- The Insights table has an **Eval** column: Stockfish's evaluation of the position after
  each listed move, computed in the background at the lowest priority (behind hovering, the
  heatmap and Analyze Game — a new `IDLE` request kind in the engine scheduler).
- Insights and Review are tabs instead of pop-up windows; the promotion chooser is drawn on
  the board; coordinates are drawn inside the squares; arrows are translucent and antialiased.
- Window geometry, state and the recent-files list are remembered.
- Qt layer tests run headless with the `offscreen` platform; CI installs the Qt runtime
  on Linux.

### Changed
- Opening Insights looks for *challenging* moves. For each of your moves it finds the
  opponent's most reliable reply (Wilson-adjusted, so a rare fluke cannot be "best") and lists
  the move when opponents fall short of that reply by at least **Delta** points on average, as
  a 95 % lower bound (**Challenge**). The old test — beating the other moves in the same
  position — was dominated by forced moves and junk alternatives (a 99.9 % recapture got an
  "edge" of +30 for beating the blunders). The columns are now Eval, Played, Score, Best
  reply and Challenge.

### Removed
- The tkinter interface (`board_ui.py`, `panels/`); `UiDispatcher` is replaced by
  `QueueDispatcher` (tests) and `ui/dispatcher.QtDispatcher` (the app).

## 1.0.0 — 2026-10-06

A review-and-modernise release: every feature kept, most of the code rewritten.

### Fixed
- Double-clicking a piece during the move animation could push the move twice and corrupt
  the game.
- Light and dark squares were swapped (a1 was light).
- Closing the window could hang while the engine was busy; the engine process is now stopped
  and quit within a bounded time, and restarted automatically if it crashes.
- Hovering a piece cancelled the running heatmap and could stall Analyze Game.
- Analyze Game never finished on games ending in checkmate or stalemate; it now runs in the
  background without moving the board and completes on any game.
- Mate scores displayed as `+300.0`; they now show as `M3`, `-M2` or `#`.
- Inaccuracies were marked `!?` instead of `?!`; move quality is now judged by the drop in
  winning chances (Lichess scale), not raw centipawns.
- Opening Insights replayed its lines from the standard start even when started from a
  later position, producing illegal positions; results now replay correctly and extend the
  loaded game.
- Opening Insights silently reported "0 notable moves" on authentication, rate-limit and
  network errors; errors are now reported, 429s are waited out, and transpositions are not
  revisited.
- The Game Review window leaked a global mouse-wheel binding after closing.
- Arrow keys navigated the game while typing in the FEN or token fields.
- The opening explorer could get stuck "Loading…" after a failed request.
- Flipping the board lost the best-move/PV arrows and never flipped the eval bar.
- Stale evaluations lingered after playing a new line mid-game or loading a new game.
- Several move explanations were wrong (exchange direction, "removes defender", outposts,
  passed pawns, forks that give check, skewers).

### Changed
- Proper Python package: `pip install -e .`, run with `python -m chess_analyzer` or
  `chess-analyzer`; Python 3.11+.
- Engine, settings and logs live in a per-user data directory; files from the old in-tree
  locations are migrated on first start.
- Stockfish 19 (universal build) is downloaded from the pinned GitHub release and verified
  against a SHA-256 digest; `STOCKFISH_PATH`/`--engine` overrides it. Linux and macOS
  downloads are supported.
- One Stockfish process with a priority scheduler (hover > heatmap > game analysis); engine
  `Threads` and `Hash` are configurable.
- Opening Insights uses a Wilson 95 % lower bound on the score rate (wins + ½ draws) against
  the rest of the position, with caps on positions, depth and time.
- Engine Lines show the engine's real continuation for each candidate move.
- Save Image renders the board (and eval bar) instead of grabbing the screen.
- Piece images are bundled (Cburnett, CC BY-SA 3.0) instead of downloaded from Wikimedia.
- Depth, filters, flip/arrow/heatmap toggles, window geometry and the last PGN folder are
  remembered between sessions.
- Logging to `logs/chess_analyzer.log`; a test suite, `ruff`, and CI on Windows and Linux.
