# Security Policy

## Supported versions

Security fixes go into the latest release on the `main` branch.

| Version | Supported |
| ------- | --------- |
| latest (main) | :white_check_mark: |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems. Instead:

- Open a [GitHub Security Advisory](https://github.com/AntonyParks/chess-analyzer/security/advisories/new)
  in this repository, or
- contact the maintainer directly via GitHub: [@AntonyParks](https://github.com/AntonyParks)

Include a description, steps to reproduce, the potential impact and, if you have one, a
suggested fix. You will get an acknowledgement as soon as the report is read, and a timeline
once it is confirmed.

## What to know about this app

- **Lichess token.** Your personal API token is stored in plain text in `config.json` inside
  the per-user data directory (`%LOCALAPPDATA%\ChessAnalyzer` on Windows; see the README for
  other platforms). It is only ever sent to `explorer.lichess.ovh` as a bearer token. It is
  never written to the log. Create the token without any scopes; if you think it leaked,
  revoke it at [lichess.org/account/oauth/token](https://lichess.org/account/oauth/token).
- **Engine download.** Stockfish is downloaded from the official GitHub release at a pinned
  version, written to a temporary file, checked against a SHA-256 digest pinned in
  `engine_setup.py`, and only then moved into `engines/` and started. A mismatch aborts the
  setup and leaves nothing behind.
- **Engine override.** `STOCKFISH_PATH` (or `--engine`, or the `engine_path` setting) makes
  the app run whatever binary you point it at. Only use engines you trust.
- **Network.** The app talks to `github.com` (engine download, first run only) and
  `explorer.lichess.ovh` (opening data, only when you enable those features). Nothing else.
