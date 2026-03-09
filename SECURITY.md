# Security Policy

## Supported Versions

This project is currently in active development. Security updates are applied to the latest version on the `main` branch.

| Version | Supported          |
| ------- | ------------------ |
| latest (main) | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in Chess Analyzer, please **do not** open a public GitHub issue.

Instead, report it privately by:
- Opening a [GitHub Security Advisory](https://github.com/AntonyParks/chess-analyzer/security/advisories/new) in this repository
- Or contacting the maintainer directly via GitHub: [@AntonyParks](https://github.com/AntonyParks)

Please include:
- A description of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Any suggested fixes (optional)

You can expect an acknowledgment within **72 hours** and a resolution timeline once the issue is confirmed.

## Notes

- This tool uses a Lichess API token stored locally. Never commit your API token to the repository.
- Stockfish is run as a subprocess; ensure you are using a trusted binary from the [official Stockfish source](https://stockfishchess.org/).
