# Changelog

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
