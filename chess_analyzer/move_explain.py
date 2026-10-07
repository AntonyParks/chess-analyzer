"""Plain-English notes about what a move does (pure chess logic, no UI).

Heuristics, not analysis: they describe the immediate, visible effect of a move — captures,
checks, forks, pins, skewers, development, outposts, files opened, passed pawns.
"""

from __future__ import annotations

from collections.abc import Sequence

import chess

VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 100,
}
CENTER = {chess.E4, chess.D4, chess.E5, chess.D5}
_DIAGONALS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
_ORTHOGONALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_RAYS = {
    chess.BISHOP: _DIAGONALS,
    chess.ROOK: _ORTHOGONALS,
    chess.QUEEN: _DIAGONALS + _ORTHOGONALS,
}
SEPARATOR = "  ·  "


def piece_name(piece_type: chess.PieceType) -> str:
    return chess.piece_name(piece_type).capitalize()


def explain_move(board: chess.Board, move: chess.Move) -> str:
    """Describe ``move`` played on ``board`` (which must be a legal move)."""
    mover = board.piece_at(move.from_square)
    if mover is None or move not in board.legal_moves:
        return ""
    us, them = board.turn, not board.turn
    target = board.piece_at(move.to_square)  # None for en passant
    after = board.copy()
    after.push(move)
    if after.is_checkmate():
        return "Checkmate!"

    if board.is_castling(move):
        side = "kingside" if board.is_kingside_castling(move) else "queenside"
        note = f"Castles {side}"
        return f"{note}{SEPARATOR}Gives check" if after.is_check() else note

    parts: list[str] = []
    if board.is_en_passant(move):
        parts.append("En passant capture")
    if move.promotion:
        parts.append(f"Promotes to {piece_name(move.promotion)}")
    if target is not None:
        parts.extend(_capture_notes(board, after, move, mover, target, us, them))
    parts.extend(_check_notes(after, move))
    parts.extend(_tactic_notes(board, after, move, mover, them))
    parts.extend(_positional_notes(board, after, move, mover, target, us, them))
    return SEPARATOR.join(parts) if parts else "Improves position"


def explain_line(board: chess.Board, moves: Sequence[chess.Move], limit: int = 5) -> str:
    """One line per move of a variation, stopping at the first illegal move."""
    lines: list[str] = []
    b = board.copy()
    for i, move in enumerate(moves[:limit]):
        if move not in b.legal_moves:
            break
        dots = "." if b.turn == chess.WHITE else "..."
        label = f"{b.fullmove_number}{dots} {b.san(move)}"
        prefix = "▶ " if i == 0 else "  "
        lines.append(f"{prefix}{label}: {explain_move(b, move)}")
        b.push(move)
    return "\n".join(lines)


# ── helpers ───────────────────────────────────────────────────────────────────


def _capture_notes(
    board: chess.Board,
    after: chess.Board,
    move: chess.Move,
    mover: chess.Piece,
    target: chess.Piece,
    us: chess.Color,
    them: chess.Color,
) -> list[str]:
    parts: list[str] = []
    attacker_val = VALUES[mover.piece_type]
    target_val = VALUES[target.piece_type]
    name = piece_name(target.piece_type)
    if not board.is_attacked_by(them, move.to_square):
        parts.append(f"Takes free {name}")
    elif target_val > attacker_val:
        if attacker_val == 3 and target_val == 5:
            parts.append("Wins the exchange")
        else:
            parts.append(f"Wins material ({name} for {piece_name(mover.piece_type)})")
    elif target_val == attacker_val:
        parts.append(f"Trades {name}")
    else:
        parts.append(f"Captures {name}")

    # Did the captured piece guard something that is now attacked and undefended?
    for sq in board.attacks(move.to_square):
        guarded = board.piece_at(sq)
        if guarded is None or guarded.color != them:
            continue
        if after.is_attacked_by(us, sq) and not after.is_attacked_by(them, sq):
            parts.append(f"Removes the defender of {piece_name(guarded.piece_type)}")
            break
    return parts


def _check_notes(after: chess.Board, move: chess.Move) -> list[str]:
    if not after.is_check():
        return []
    checkers = after.checkers()
    if len(checkers) >= 2:
        return ["Double check!"]
    if move.to_square not in checkers:
        return ["Discovered check"]
    return ["Gives check"]


def _tactic_notes(
    board: chess.Board,
    after: chess.Board,
    move: chess.Move,
    mover: chess.Piece,
    them: chess.Color,
) -> list[str]:
    parts: list[str] = []
    piece_type = move.promotion or mover.piece_type
    attacker_val = VALUES[piece_type]

    # Forks / attacks: enemy pieces the moved piece now threatens for real — the king,
    # anything worth more than the attacker, or anything left undefended.
    threatened: list[chess.Piece] = []
    for sq in after.attacks(move.to_square):
        p = after.piece_at(sq)
        if p is None or p.color != them or VALUES[p.piece_type] < 3:
            continue
        if (
            p.piece_type == chess.KING
            or VALUES[p.piece_type] > attacker_val
            or not after.is_attacked_by(them, sq)
        ):
            threatened.append(p)
    threatened.sort(key=lambda p: -VALUES[p.piece_type])
    if len(threatened) >= 2:
        names = " and ".join(piece_name(p.piece_type) for p in threatened[:2])
        parts.append(f"Forks {names}")
    elif len(threatened) == 1 and threatened[0].piece_type != chess.KING:
        parts.append(f"Attacks {piece_name(threatened[0].piece_type)}")

    # New absolute pin against the enemy king.
    for sq in chess.SquareSet(after.occupied_co[them]):
        p = after.piece_at(sq)
        if p is None or p.piece_type == chess.KING:
            continue
        if after.is_pinned(them, sq) and not board.is_pinned(them, sq):
            parts.append(f"Pins {piece_name(p.piece_type)} to the King")
            break

    skewer = _skewer_note(after, move.to_square, piece_type, them)
    if skewer:
        parts.append(skewer)
    return parts


def _skewer_note(
    after: chess.Board, square: chess.Square, piece_type: chess.PieceType, them: chess.Color
) -> str | None:
    """A valuable enemy piece on a ray with a cheaper enemy piece behind it."""
    rays = _RAYS.get(piece_type)
    if not rays:
        return None
    for df, dr in rays:
        f, r = chess.square_file(square) + df, chess.square_rank(square) + dr
        front: chess.Piece | None = None
        while 0 <= f < 8 and 0 <= r < 8:
            p = after.piece_at(chess.square(f, r))
            if p is not None:
                if front is None:
                    if p.color != them or VALUES[p.piece_type] < 5:
                        break
                    front = p
                else:
                    if (
                        p.color == them
                        and p.piece_type != chess.KING
                        and 3 <= VALUES[p.piece_type] < VALUES[front.piece_type]
                    ):
                        return (
                            f"Skewers {piece_name(front.piece_type)} "
                            f"to win {piece_name(p.piece_type)}"
                        )
                    break
            f += df
            r += dr
    return None


def _positional_notes(
    board: chess.Board,
    after: chess.Board,
    move: chess.Move,
    mover: chess.Piece,
    target: chess.Piece | None,
    us: chess.Color,
    them: chess.Color,
) -> list[str]:
    parts: list[str] = []
    back_rank = 0 if us == chess.WHITE else 7
    to_rank = chess.square_rank(move.to_square)

    if (
        mover.piece_type in (chess.KNIGHT, chess.BISHOP)
        and chess.square_rank(move.from_square) == back_rank
    ):
        parts.append(f"Develops {piece_name(mover.piece_type)}")

    in_their_half = to_rank >= 4 if us == chess.WHITE else to_rank <= 3
    if (
        mover.piece_type == chess.KNIGHT
        and in_their_half
        and _supported_by_pawn(after, move.to_square, us)
        and not _enemy_pawn_ahead(after, move.to_square, us, them, adjacent_only=True)
    ):
        parts.append("Establishes outpost")

    if move.to_square in CENTER and target is None and mover.piece_type != chess.KING:
        parts.append("Controls center")

    opened: list[int] = []
    captured = target is not None or board.is_en_passant(move)
    if mover.piece_type == chess.PAWN and captured:
        opened.append(chess.square_file(move.from_square))
    if target is not None and target.piece_type == chess.PAWN:
        opened.append(chess.square_file(move.to_square))
    for file in dict.fromkeys(opened):
        if not any(
            after.piece_at(chess.square(file, r)) for r in range(8) if _is_pawn(after, file, r)
        ):
            parts.append(f"Opens the {chess.FILE_NAMES[file]}-file")

    if (
        mover.piece_type == chess.PAWN
        and not move.promotion
        and _is_passed(after, move.to_square, us, them)
        and not _is_passed(board, move.from_square, us, them)
    ):
        parts.append("Creates passed pawn")
    return parts


def _is_pawn(board: chess.Board, file: int, rank: int) -> bool:
    p = board.piece_at(chess.square(file, rank))
    return p is not None and p.piece_type == chess.PAWN


def _supported_by_pawn(board: chess.Board, square: chess.Square, us: chess.Color) -> bool:
    return any(
        (p := board.piece_at(sq)) is not None and p.piece_type == chess.PAWN
        for sq in board.attackers(us, square)
    )


def _enemy_pawn_ahead(
    board: chess.Board,
    square: chess.Square,
    us: chess.Color,
    them: chess.Color,
    adjacent_only: bool,
) -> bool:
    """Is there an enemy pawn on a rank ahead of ``square`` (on adjacent files, or any of the
    three files around it)?"""
    f, r = chess.square_file(square), chess.square_rank(square)
    ranks = range(r + 1, 8) if us == chess.WHITE else range(0, r)
    files = (f - 1, f + 1) if adjacent_only else (f - 1, f, f + 1)
    for ff in files:
        if not 0 <= ff < 8:
            continue
        for rr in ranks:
            p = board.piece_at(chess.square(ff, rr))
            if p is not None and p.color == them and p.piece_type == chess.PAWN:
                return True
    return False


def _is_passed(
    board: chess.Board, square: chess.Square, us: chess.Color, them: chess.Color
) -> bool:
    return not _enemy_pawn_ahead(board, square, us, them, adjacent_only=False)
