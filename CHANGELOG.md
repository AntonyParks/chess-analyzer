# Changelog

## Unreleased

### Added
- **Opening repertoire** (a new **Repertoire** tab, F8): build a White and a Black repertoire on
  the board with Lichess game counts and Stockfish evaluations for every candidate move;
  transpositions marked (↪ joins …); coverage gaps flagged and **Go to biggest gap**; spaced-
  repetition training with opponent replies weighted by real games; **Check my games** finds
  where your chess.com games left the repertoire. Stored in `repertoire.json`.
- Lichess rating filters now include 1000, 1200 and 1400.
- **Puzzles from your games** (a new **Puzzles** tab, F7): downloads your recent chess.com
  games (public API, no login), finds the moves where you gave up winning chances and turns
  each into a puzzle where you look for the better move. Severity filters (blunders,
  mistakes, inaccuracies), a **Lookahead** difficulty slider (only mistakes whose punishment
  shows within N moves), multi-move lines while the engine line stays forcing, near-best
  answers accepted, hints, solutions, and a **Retry failed** mode. Results persist in
  `puzzles.json`; scanned games are not scanned again.
- Puzzles work from a local library of all your chess.com games (`chesscom_games.json`):
  the first run downloads and scans your whole history; **Refresh** then fetches only the
  current month and new months and scans only unscanned games.
- Puzzle **Order**: newest first, easiest first or hardest first (difficulty from the
  lookahead needed, line length, number of accepted answers and size of the mistake).
- **Export for phone**: every puzzle in one self-contained web page (filters, lookahead,
  ordering, multi-move lines, hints, retry; tap or drag to move), with results kept on the
  phone. Also `python -m chess_analyzer.phone_export`.
- The board has a puzzle mode (no engine requests or scores, user moves checked before they
  are played) and can swap in a separate game state, so puzzles never disturb the loaded
  game or its analysis.

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
- Insights has a **Max positions** control (the search budget, one position per second) and
  reports how many plies deep it got, and which limit stopped it, in the status line.
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
