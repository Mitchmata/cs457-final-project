"""Connect 4 game engine (6 rows x 7 columns).

Authoritative rules per docs/fsm_specification.md section 3. Only
Connect4Engine.drop_token() mutates the board and the turn pointer, and it
leaves state completely untouched whenever a guard rejects the move.
"""

from __future__ import annotations

ROWS = 6
COLS = 7
EMPTY = " "

# (row delta, col delta): horizontal, vertical, descending \, ascending /
DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))


def render_board(board: list[list[str]]) -> str:
    """Return an ASCII drawing of a board matrix with column headers."""
    border = "+" + "---+" * COLS
    lines = ["  " + "   ".join(str(c) for c in range(COLS)), border]
    for row in board:
        lines.append("|" + "|".join(f" {cell} " for cell in row) + "|")
        lines.append(border)
    return "\n".join(lines)


class Connect4Engine:
    """Server-side Connect 4 rules engine."""

    def __init__(self, p1_name: str, p2_name: str) -> None:
        self.p1 = p1_name
        self.p2 = p2_name
        self.roles = {p1_name: "X", p2_name: "O"}
        self.board = [[EMPTY] * COLS for _ in range(ROWS)]
        self.current_turn = p1_name
        self.move_count = 0

    def opponent_of(self, name: str) -> str:
        """Return the other player's alias."""
        return self.p2 if name == self.p1 else self.p1

    def board_copy(self) -> list[list[str]]:
        """Return a deep copy of the board suitable for serialization."""
        return [row[:] for row in self.board]

    def drop_token(self, player_id: str, col: int) -> tuple[bool, str, dict]:
        """Attempt a move. Returns (accepted, status, info).

        Rejections return (False, ERROR_CODE, {}) with no state change.
        Accepted moves return status WIN, DRAW, or CONTINUE and an info dict
        with row, col, role (plus winning_coords for WIN).
        """
        if player_id != self.current_turn:
            return False, "ERR_OUT_OF_TURN", {}
        if isinstance(col, bool) or not isinstance(col, int) \
                or not 0 <= col < COLS:
            return False, "ERR_INVALID_COLUMN", {}
        if self.board[0][col] != EMPTY:
            return False, "ERR_COLUMN_FULL", {}

        # Gravity: lowest empty row, searching from the bottom upward.
        row = next(r for r in range(ROWS - 1, -1, -1)
                   if self.board[r][col] == EMPTY)
        token = self.roles[player_id]
        self.board[row][col] = token
        self.move_count += 1
        info = {"row": row, "col": col, "role": token}

        line = self._winning_line(row, col, token)
        if line:
            info["winning_coords"] = line
            return True, "WIN", info
        if self.move_count == ROWS * COLS:
            return True, "DRAW", info

        self.current_turn = self.opponent_of(player_id)
        return True, "CONTINUE", info

    def _winning_line(self, row: int, col: int, token: str) -> list[list[int]]:
        """Return four connected [row, col] cells through (row, col), or []."""
        for d_row, d_col in DIRECTIONS:
            line = [(row, col)]
            for sign in (1, -1):
                r, c = row + sign * d_row, col + sign * d_col
                while 0 <= r < ROWS and 0 <= c < COLS \
                        and self.board[r][c] == token:
                    if sign == 1:
                        line.append((r, c))
                    else:
                        line.insert(0, (r, c))
                    r, c = r + sign * d_row, c + sign * d_col
            if len(line) >= 4:
                idx = line.index((row, col))
                start = min(max(0, idx - 3), len(line) - 4)
                return [list(cell) for cell in line[start:start + 4]]
        return []

    def render_ascii(self) -> str:
        """ASCII rendering of the current board."""
        return render_board(self.board)
