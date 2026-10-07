# Chess Analyzer

A desktop chess analysis tool built with Python and tkinter. Hover over pieces to see
Stockfish evaluations, explore move heatmaps, analyse whole games, and look up what real
players have played from any position.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![CI](https://github.com/AntonyParks/chess-analyzer/actions/workflows/ci.yml/badge.svg)](https://github.com/AntonyParks/chess-analyzer/actions/workflows/ci.yml)

![Chess Analyzer](docs/screenshot.png)

*Hovering the f3 knight: Stockfish scores each of its moves, the heatmap bands every piece
best-to-worst, the gold arrow marks the engine's top choice (castling), Engine Lines list the
best continuations, and the Opening Explorer shows what Lichess players did from here.*

## Features

- **Hover evaluation** — hover over any piece to see Stockfish's score for every legal move
- **Piece heatmap** — colour-coded bands on each piece from best (gold) to worst (red)
- **Position eval bar** and **eval graph** — who is winning now, and over the whole game
- **Engine Lines** — the top three moves with the engine's continuation; click one to see the
  line on the board with a plain-English note for each move
- **Analyze Game** — evaluates every position of the loaded game in the background (you can
  keep browsing), marks inaccuracies, mistakes and blunders in the move list, and builds a
  **Game Review** with a better alternative for each flagged move
- **Move history** — clickable, colour-coded move list; arrow keys and the eval graph also navigate
- **Opening Explorer** — what moves real players have made from the current position
  (Lichess data; needs a free API token)
- **Opening Insights** — walks the Lichess opening tree from the current position and
  surfaces moves whose results are *significantly* better than the alternatives (see below)
- **Click or drag** to move pieces; promotion is chosen on the board (or press `q`/`r`/`b`/`n`)
- **PGN import/export** (headers preserved), **FEN load/copy**, **Save Image** (rendered, not a
  screen grab)
- **Board flip**, best-move arrow, adjustable search depth
- Stockfish 19 **downloads automatically** on first run

## Requirements

- Python 3.11 or newer
- Windows, macOS or Linux. Stockfish downloads automatically on Windows (x64 and ARM64),
  Linux (x64 and ARM64) and macOS; on anything else, install Stockfish yourself and point
  `STOCKFISH_PATH` at it (see below). Developed and tested on Windows 11.

## Installation

```bash
git clone https://github.com/AntonyParks/chess-analyzer.git
cd chess-analyzer
python -m venv .venv
.venv\Scripts\activate        # Windows   (source .venv/bin/activate elsewhere)
pip install -e .
python -m chess_analyzer      # or simply: chess-analyzer
```

The first start downloads Stockfish 19 (about 80 MB) into your data directory, checks it
against a pinned SHA-256 and verifies it answers UCI before using it. Later starts reuse it.

## Where things live

Runtime files are kept out of the source tree, in a per-user data directory:

| Platform | Data directory |
|----------|----------------|
| Windows  | `%LOCALAPPDATA%\ChessAnalyzer` |
| macOS    | `~/Library/Application Support/ChessAnalyzer` |
| Linux    | `$XDG_DATA_HOME/chess-analyzer` or `~/.local/share/chess-analyzer` |

Inside it: `engines/` (Stockfish), `config.json` (settings, including your Lichess token in
plain text) and `logs/chess_analyzer.log`. Files from older versions that lived inside the
package folder are moved there automatically on first start.

Environment variables and command-line switches:

| Setting | Effect |
|---------|--------|
| `CHESS_ANALYZER_DATA_DIR` / `--data-dir DIR` | use another data directory |
| `STOCKFISH_PATH` / `--engine PATH` | use this engine binary instead of downloading one (any UCI engine works) |
| `CHESS_ANALYZER_LOG_LEVEL` / `--log-level LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |

`config.json` also holds `engine_threads` (0 = half your CPUs) and `engine_hash_mb` (256) if
you want to tune the engine. To upgrade a manually placed engine, delete it from `engines/`
and restart — the newest binary in that folder is the one used.

## Lichess data features (optional)

Both features read move statistics from billions of Lichess games and need a personal API
token (the explorer endpoint rejects anonymous requests):

1. Create a free token at [lichess.org/account/oauth/token](https://lichess.org/account/oauth/token)
   — no scopes are needed.
2. Tick **Opening Explorer**, paste the token into the field and click **Save**.

**Opening Explorer** shows, for the position on the board, how often each move was played and
how it scored, filtered by time control and rating. Double-click a row to play that move.

**Opening Insights** (button in the Eval Display panel) searches the opening tree from the
current position, one request per second, and lists moves for your colour that *beat the
field*. A move is listed when the lower end of a 95 % confidence interval around its score
rate (wins plus half the draws) is at least **Delta** points above the score rate of the
other games in the same position — so rare moves with a lucky record do not qualify, but
well-supported ones do. **Min games** sets the minimum sample, **Min %** and **Top N** limit
how deep and wide the search goes. Select a row to replay the line on the board; the move is
drawn as a purple arrow.

## Keyboard and mouse

| Action | How |
|--------|-----|
| Move a piece | click source then destination, or drag |
| Promote | click the piece in the chooser, or press `q` `r` `b` `n` (`Esc` cancels) |
| Navigate | `←` `→` `Home` `End`, the ⏮ ◀ ▶ ⏭ buttons, the move list, or the eval graph |
| Show a line | click an entry in Engine Lines |

## Development

```bash
pip install -e .[dev]
ruff check . && ruff format --check .
python -m pytest
```

The pure logic (engine scheduling, evaluation formatting, move explanations, game state,
Lichess client, Insights statistics, PGN handling, the rendered board image) is covered by
the test suite and runs headless; the tkinter layer is thin by design. Layout:

| Module | Role |
|--------|------|
| `app.py` | the window, panels wiring, settings |
| `board_ui.py`, `game_state.py` | the board widget and the game it shows |
| `engine_worker.py`, `engine_setup.py` | Stockfish process and scheduling; finding/downloading the engine |
| `evaluation.py`, `move_explain.py` | score formatting/ranking; plain-English move notes |
| `lichess_api.py`, `opening_insights.py` | Lichess client; the Insights crawl and statistics |
| `panels/` | history + eval graph, engine lines, explorer, insights, analysis/review |
| `ui_dispatch.py` | the only way background threads reach Tk |

## Dependencies and credits

- [`chess`](https://python-chess.readthedocs.io/) (python-chess) — board logic, PGN, UCI
- [`pillow`](https://pillow.readthedocs.io/) — piece images and Save Image
- [Stockfish](https://stockfishchess.org/) — downloaded from the official GitHub release
- Piece images by [Cburnett](https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces),
  CC BY-SA 3.0 (see `chess_analyzer/assets/pieces/ATTRIBUTION.md`)
- Opening data from [Lichess](https://lichess.org/)

## License

MIT — see [LICENSE](LICENSE). The piece images are CC BY-SA 3.0 as noted above.
