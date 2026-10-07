import chess

from chess_analyzer import theme
from chess_analyzer.board_image import BAR_WIDTH, GAP, render
from chess_analyzer.pieces import PieceImages


def _rgb(colour: str) -> tuple[int, int, int]:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _square_centre(file: int, rank: int, square: int, flipped: bool) -> tuple[int, int]:
    margin = max(14, square // 4)
    left = BAR_WIDTH + GAP + margin
    col = 7 - file if flipped else file
    row = rank if flipped else 7 - rank
    return left + col * square + square // 2, 4 + row * square + square // 2


def test_render_size_and_square_colours():
    pieces = PieceImages()
    image = render(chess.Board(), pieces=pieces, square=40)
    assert image.mode == "RGB"
    assert image.size == (BAR_WIDTH + GAP + 14 + 8 * 40 + 4, 4 + 8 * 40 + 14)
    # Empty squares only: e4 is light, d4 is dark.
    assert image.getpixel(_square_centre(4, 3, 40, False)) == _rgb(theme.LIGHT_SQ)
    assert image.getpixel(_square_centre(3, 3, 40, False)) == _rgb(theme.DARK_SQ)


def test_a1_is_dark_and_flipping_moves_it_to_the_top_right():
    pieces = PieceImages()
    board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    normal = render(board, pieces=pieces, square=40, with_eval_bar=False)
    flipped = render(board, pieces=pieces, square=40, flipped=True, with_eval_bar=False)
    margin = 14
    sq = 40

    def centre(col: int, row: int) -> tuple[int, int]:  # board starts at x = margin (no bar)
        return margin + col * sq + sq // 2, 4 + row * sq + sq // 2

    assert normal.getpixel(centre(0, 7)) == _rgb(theme.DARK_SQ)  # a1 bottom-left
    assert normal.getpixel(centre(7, 7)) == _rgb(theme.LIGHT_SQ)  # h1 bottom-right
    assert flipped.getpixel(centre(7, 0)) == _rgb(theme.DARK_SQ)  # a1 now top-right
    # Square colours are symmetric under rotation, so use the white king on e1 to tell the
    # orientations apart: it sits at (col 4, row 7) normally and at (col 3, row 0) flipped.
    assert normal.getpixel(centre(4, 7)) != _rgb(theme.DARK_SQ)
    assert normal.getpixel(centre(3, 0)) == _rgb(theme.DARK_SQ)
    assert flipped.getpixel(centre(3, 0)) != _rgb(theme.DARK_SQ)


def test_pieces_heatmap_and_bar_render_without_error():
    pieces = PieceImages()
    board = chess.Board()
    move = chess.Move.from_uci("e2e4")
    image = render(
        board,
        pieces=pieces,
        square=48,
        last_move=move,
        position_cp=150,
        best_move=chess.Move.from_uci("g1f3"),
        heatmap={chess.E2: [(move, 0.3), (chess.Move.from_uci("e2e3"), 0.1)]},
        rank_map={"e2e4": 1.0, "e2e3": 0.5},
    )
    # Something was drawn over the e2 pawn's square (heatmap gold band at the top).
    x, y = _square_centre(4, 1, 48, False)
    assert image.getpixel((x, y - 20)) == _rgb(theme.GOLD)
