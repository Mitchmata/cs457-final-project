# Server-Side Finite State Machine (FSM) Specification: Connect 4 Engine
**Game:** Networked 2-Player Turn-Based Connect 4  
**Board Dimensions:** 6 Rows $\times$ 7 Columns (42 Slots)  
**Diagram Format:** Mermaid (`stateDiagram-v2`)  
**Target Environment:** Cisco Modeling Labs (CML) Multi-Subnet Architecture  

---

## 1. Complete Server State Transition Diagram

```mermaid
stateDiagram-v2
    [*] --> INIT

    INIT --> WAITING_FOR_PLAYERS : Server bound to 0.0.0.0:PORT & listening

    state WAITING_FOR_PLAYERS {
        [*] --> LOBBY_EMPTY
        LOBBY_EMPTY --> ONE_PLAYER_CONNECTED : Client 1 connects (C4P: CONNECT) / Send LOBBY_WAIT
        ONE_PLAYER_CONNECTED --> LOBBY_EMPTY : Client 1 disconnects (EOF/RST) / Free slot
        ONE_PLAYER_CONNECTED --> READY_TO_START : Client 2 connects (C4P: CONNECT)
    }

    WAITING_FOR_PLAYERS --> GAME_START : Two valid players registered

    state GAME_START {
        [*] --> ASSIGN_ROLES
        ASSIGN_ROLES --> INIT_GRID : Assign P1='X', P2='O'
        INIT_GRID --> BROADCAST_START : Clear 6x7 board (42 cells) / Set current_turn = P1
    }

    GAME_START --> PLAYER_TURN : Send GAME_START to both clients

    state PLAYER_TURN {
        [*] --> AWAITING_MOVE
        AWAITING_MOVE --> REJECT_OUT_OF_TURN : Inactive player sends MOVE
        REJECT_OUT_OF_TURN --> AWAITING_MOVE : Send ERROR (ERR_OUT_OF_TURN)
        AWAITING_MOVE --> RECEIVE_MOVE : Active player sends MOVE (col)
    }

    PLAYER_TURN --> EVALUATE_MOVE : Active client submits column index

    state EVALUATE_MOVE {
        [*] --> VALIDATE_COLUMN
        VALIDATE_COLUMN --> REJECT_INVALID_COL : col < 0 or col > 6
        REJECT_INVALID_COL --> [*] : Send ERROR (ERR_INVALID_COLUMN)
        
        VALIDATE_COLUMN --> CHECK_COLUMN_SPACE : 0 <= col <= 6
        CHECK_COLUMN_SPACE --> REJECT_FULL_COL : Top slot (row 0) occupied
        REJECT_FULL_COL --> [*] : Send ERROR (ERR_COLUMN_FULL)

        CHECK_COLUMN_SPACE --> APPLY_GRAVITY : Unoccupied slot exists
        APPLY_GRAVITY --> UPDATE_STATE : Drop token to lowest available row
        UPDATE_STATE --> CHECK_WIN : Evaluate 4-in-a-row (H, V, D1, D2)
        
        CHECK_WIN --> WIN_DETECTED : 4 consecutive matching tokens found
        CHECK_WIN --> CHECK_DRAW : No 4-in-a-row
        
        CHECK_DRAW --> DRAW_DETECTED : All 42 slots filled (Row 0 full across all columns)
        CHECK_DRAW --> CONTINUE_GAME : Grid contains available slots
    }

    EVALUATE_MOVE --> PLAYER_TURN : Valid move, no win/draw / Send STATE_UPDATE & switch turn
    EVALUATE_MOVE --> PLAYER_TURN : Invalid move / Active player re-prompted

    EVALUATE_MOVE --> GAME_OVER : Win detected (WIN) or Grid saturated (DRAW)

    PLAYER_TURN --> GAME_OVER : Client sends DISCONNECT or connection dropped (EOF / RST)
    EVALUATE_MOVE --> GAME_OVER : Client drops during move evaluation

    state GAME_OVER {
        [*] --> RESOLVE_OUTCOME
        RESOLVE_OUTCOME --> NOTIFY_PLAYERS : Outcome = WIN | DRAW | FORFEIT
        NOTIFY_PLAYERS --> [*] : Broadcast GAME_OVER to connected sockets
    }

    GAME_OVER --> CLEANUP : Teardown active match

    state CLEANUP {
        [*] --> CLOSE_SOCKETS : Cleanly close client sockets
        CLOSE_SOCKETS --> RESET_GRID : Clear 6x7 matrix, reset player state
    }

    CLEANUP --> WAITING_FOR_PLAYERS : Server returns to listening state
    CLEANUP --> [*] : Server process termination (SIGINT/SIGTERM)
```

---

## 2. Server State Catalog & Operational Roles

| State Name | Operational Role | Acceptable Inbound Messages | Outbound Messages Generated |
| :--- | :--- | :--- | :--- |
| `INIT` | Initialize server socket, bind to `0.0.0.0:PORT`, setup non-blocking selector loop. | None | None |
| `WAITING_FOR_PLAYERS` | Accept incoming TCP handshakes; register client aliases into Player 1 and Player 2 slots. | `CONNECT`, `DISCONNECT` | `LOBBY_WAIT` |
| `GAME_START` | Assign `X` to Player 1 and `O` to Player 2, initialize the 6x7 board matrix, set initial turn. | None | `GAME_START` |
| `PLAYER_TURN` | Block on socket I/O for active player input; enforce turn boundaries against out-of-turn moves. | `MOVE`, `DISCONNECT` | None (or `ERROR` if out of turn) |
| `EVALUATE_MOVE` | Calculate gravity row drop $(r, c)$, verify 4-in-a-row vectors, inspect grid saturation. | None | `ERROR`, `STATE_UPDATE` |
| `GAME_OVER` | Resolve terminal game outcome (`WIN`, `DRAW`, `FORFEIT`), construct winning line coordinates. | None | `GAME_OVER` |
| `CLEANUP` | Close active peer sockets, purge match memory, reset lobby back to listening state. | None | TCP FIN teardown |

---

## 3. Game Engine Logic & State Handling Rules

### 3.1 Gravity-Based Drop Calculation
In Connect 4, players do not choose a row; they choose a column index $c \in [0, 6]$.
1. **Bounds Verification:** Ensure $0 \le c \le 6$. If out of range, dispatch `ERR_INVALID_COLUMN`.
2. **Column Saturation Check:** Check whether `board[0][c] != " "`. If true, the column is full; reject with `ERR_COLUMN_FULL`.
3. **Gravity Iteration:** Search upwards starting from the bottom row $r = 5$ to $r = 0$:
   $$\text{target\_row} = \max \{ r \in [0, 5] \mid \text{board}[r][c] = \text{" "} \}$$
4. **Token Placement:** Place the active player's assigned token: `board[target_row][c] = current_role`. Increment total move count.

### 3.2 Win & Draw Condition Algorithms
Following every successful piece placement at $(r, c)$ with token $S \in \{X, O\}$, the server evaluates:

1. **Horizontal Vector ($\leftrightarrow$):** Check for 4 matching consecutive tokens across row $r$ from column $c - 3$ to $c + 3$:
   $$\exists k \in [0, 3] : \bigwedge_{i=0}^3 \text{board}[r][c - k + i] = S$$
2. **Vertical Vector ($\updownarrow$):** Check downwards along column $c$. Since tokens drop from above, a vertical connect-4 only occurs downward:
   $$r \le 2 \land \bigwedge_{i=0}^3 \text{board}[r + i][c] = S$$
3. **Ascending Diagonal Vector (/):** Check along slope $\Delta r = -\Delta c$ spanning $(r+3, c-3)$ through $(r-3, c+3)$.
4. **Descending Diagonal Vector (\\):** Check along slope $\Delta r = \Delta c$ spanning $(r-3, c-3)$ through $(r+3, c+3)$.
5. **Draw Condition (Grid Full):** If no win is detected and total move count $= 42$ (or all cells in top row `board[0][0..6]` are not `" "`), transition to `GAME_OVER` with `outcome = "DRAW"`.
6. **Continuation:** If neither win nor draw condition is satisfied, toggle active turn to the opponent:
   $$\text{current\_turn} = (\text{current\_turn} == \text{P1}) \ ? \ \text{P2} : \text{P1}$$
   Dispatch `STATE_UPDATE` to both clients and return to `PLAYER_TURN`.

---

## 4. Error Handling, Edge Cases & Disconnect Transitions

### 4.1 Out-of-Turn Move Protection
- **Condition:** In state `PLAYER_TURN`, client `P2` sends a `MOVE` while `current_turn == P1`.
- **Handling:**
  - Transition to sub-state `REJECT_OUT_OF_TURN`.
  - Transmit an `ERROR` message with code `ERR_OUT_OF_TURN` exclusively to `P2`.
  - State machine returns immediately to `AWAITING_MOVE`.
  - The turn pointer remains on `P1`, and board state is unaltered.

### 4.2 Invalid Input Recovery
- **Condition:** The active player selects an out-of-bounds column ($c < 0$ or $c > 6$) or a saturated column (`board[0][c] != " "`).
- **Handling:**
  - Server transitions to `REJECT_INVALID_COL` or `REJECT_FULL_COL`.
  - An `ERROR` message (`ERR_INVALID_COLUMN` or `ERR_COLUMN_FULL`) is sent back to the active player.
  - The active player's turn is **not forfeit**; the state machine loops back to `PLAYER_TURN` allowing the player to select a valid column.

### 4.3 Graceful Disconnection (`DISCONNECT` Message)
- **Condition:** A connected player voluntarily quits by typing `quit` or `exit` in their CLI.
- **Handling:**
  - Client sends framed `DISCONNECT` message.
  - Server immediately transitions to `GAME_OVER`.
  - Server sets `outcome = "FORFEIT"` and declares the remaining connected player the winner (`winner_id = opponent_id`).
  - Broadcasts `GAME_OVER` to the remaining player and proceeds to `CLEANUP`.

### 4.4 Abrupt Network Drop & Socket Severance (EOF / RST)
- **Condition:** A client process is killed abruptly (`kill -9`), or link degradation/severance occurs in the CML virtual network.
- **Handling:**
  - Low-level `recv_exact()` detects 0-byte EOF (`b""`) or catches `ConnectionResetError` / `BrokenPipeError`.
  - If the drop occurs during `WAITING_FOR_PLAYERS`:
    - The server clears the occupied slot and resets the lobby to `LOBBY_EMPTY` without crashing.
  - If the drop occurs during an active match (`PLAYER_TURN` or `EVALUATE_MOVE`):
    - Server immediately transitions to `GAME_OVER`.
    - Server sets `outcome = "FORFEIT"` and `reason = "OPPONENT_DISCONNECTED"`.
    - Dispatches `GAME_OVER` to the surviving client socket.
    - Server transitions to `CLEANUP`, closes both sockets, flushes the 6x7 grid, and returns cleanly to `WAITING_FOR_PLAYERS` to await new connections.
