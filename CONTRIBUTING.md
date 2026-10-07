# Contributing to Chess Analyzer

Thank you for your interest in contributing! Here's how to get started.

## Getting started

1. Fork the repository and clone your fork:
   `git clone https://github.com/YOUR_USERNAME/chess-analyzer.git`
2. Create a virtual environment and install the project with the dev tools:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate          # Windows (source .venv/bin/activate elsewhere)
   pip install -e .[dev]
   ```
3. Run it: `python -m chess_analyzer`. The first start downloads Stockfish into your data
   directory (see the README); set `STOCKFISH_PATH` to use an engine you already have.
4. Create a branch: `git checkout -b feature/your-feature-name`

## Prerequisites

- Python 3.11+
- (Optional) a free Lichess API token for the Opening Explorer and Opening Insights

## Making changes

- Run the checks before you push — CI runs the same ones on Windows and Linux:
  ```bash
  ruff check . && ruff format --check .
  python -m pytest
  ```
  `ruff format .` fixes formatting; `ruff check --fix .` fixes the simple lint findings.
- Put logic that does not need a window into a plain module and give it a test under
  `tests/`. The tests run headless and must not import tkinter.
- Never touch Tk from a background thread: hand callables to the `UiDispatcher`
  (`dispatcher.post(fn, *args)`) and let the main thread run them.
- Engine work goes through `EngineWorker.submit(...)`; results carry the position's EPD, and
  consumers must check it before using them.
- Keep user-facing errors actionable (what happened, what to do) and log the details.
- Don't commit tokens, engines or your data directory — see `.gitignore`.

## Submitting a pull request

1. Commit with a clear, descriptive message.
2. Push to your fork and open a pull request against `main`.
3. Fill in the template: what changed, why, and how you tested it. Reference related issues
   (e.g. `Closes #12`).

## Reporting bugs

Open an [issue](https://github.com/AntonyParks/chess-analyzer/issues/new?template=bug_report.md)
using the bug template. The log file (`logs/chess_analyzer.log` in the data directory) and a
PGN or FEN that reproduces the problem help a lot.

## Feature requests

Open an issue with the feature template and describe the use case and why it would benefit
the project.

## Code of conduct

Please read and follow our [Code of Conduct](CODE_OF_CONDUCT.md).
