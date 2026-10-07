## Description

Briefly describe what this PR does and why.

Closes # (issue number, if applicable)

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / code cleanup
- [ ] Documentation update
- [ ] Other (describe below)

## Changes made

- 
- 

## Testing

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `python -m pytest` passes (new logic has tests)
- [ ] Ran the app and exercised the affected area:
  - [ ] hover evaluation / heatmap / eval bar
  - [ ] moving pieces (click, drag, promotion), navigation, PGN/FEN loading
  - [ ] Analyze Game / Game Review / Engine Lines
  - [ ] Opening Explorer / Opening Insights (Lichess)
  - [ ] engine download or `STOCKFISH_PATH` override
- [ ] No functional changes (docs/formatting only)

## Screenshots (if applicable)

<!-- Add screenshots here if the UI was changed -->

## Checklist

- [ ] No tokens, engines or data-directory files are committed
- [ ] Background threads reach Tk only through the `UiDispatcher`
- [ ] Documentation updated where behaviour changed (README, CHANGELOG)
