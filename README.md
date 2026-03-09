# Chess Analyzer

A desktop chess analysis tool built with Python and tkinter. Hover over pieces to see Stockfish evaluations, explore move heatmaps, and look up what moves real players have made from any position.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)

## Features

- **Hover evaluation** — hover over any piece to see Stockfish's score for every legal move
- **Piece heatmap** — color-coded bands on each piece showing best (gold) to worst (red) moves
- **Position eval bar** — vertical bar showing who is winning
- **Move history** — scrollable, color-coded move list; click any move to jump to that position
- **Opening Explorer** — see what moves real players have made from the current position (requires free Lichess API token)
- **Incremental analysis** — board colors update move-by-move as Stockfish finishes each line
- **Click or drag** to move pieces
- **PGN import/export** and FEN load/copy
- **Board flip**, best-move arrow, arrow key navigation
- Stockfish 17 **downloads automatically** on first run

## Requirements

- Python 3.10+
- Windows (tested on Windows 11)

## Installation

```bash
git clone https://github.com/AntonyParks/chess-analyzer.git
cd chess-analyzer
pip install -r requirements.txt
python chess_analyzer/main.py
```

Stockfish will download automatically the first time you run the app (~60 MB, one-time).

## Opening Explorer (optional)

The Opening Explorer shows move frequency data from millions of Lichess games. To enable it:

1. Create a free API token at [lichess.org/account/oauth/token](https://lichess.org/account/oauth/token) (no special permissions needed)
2. Check **Opening Explorer** in the app
3. Paste your token into the field and click **Save**

## Dependencies

- [`chess`](https://python-chess.readthedocs.io/) — board logic, move generation, PGN parsing
- Stockfish 17 — downloaded automatically to `chess_analyzer/engines/`
