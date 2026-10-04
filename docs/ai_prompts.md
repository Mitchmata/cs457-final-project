# AI Prompting & Constraint Enforcement Strategy: Connect 4 Network Engine

**Sprint:** Sprint 1 — Application Protocol & Game State Machine (FSM) Design  
**Target Application:** 2-Player Turn-Based Connect 4 CLI (C4P Protocol)  
**Governing Policy:** AI-Use & Design-First Policy  

---

## 1. Architectural Strategy & Constraint-First Methodology

Modern AI coding assistants (ChatGPT, Claude, GitHub Copilot) routinely generate generic, unstructured socket boilerplate when prompted without strict boundaries. Common failure patterns include:
- Relying on `sock.recv(1024)` directly under the false assumption that one TCP `send()` equals one `recv()`.
- Defaulting to HTTP, WebSockets, or high-level async frameworks explicitly forbidden by the assignment specifications.
- Using native-endian byte ordering or variable-width delimiters (`\n`, `|`) instead of the required Network Byte Order framing.
- Inadequately handling POSIX TCP 0-byte EOF conditions, resulting in infinite CPU-spinning loops upon client disconnection.

To prevent these defects and satisfy the assignment's design-first mandate, all AI prompts in this project must be wrapped in a **Master System Constraint Envelope** that binds the assistant strictly to `docs/protocol_blueprint.md` and `docs/fsm_specification.md`.

---

## 2. Master System Constraint Envelopes

Every generative AI coding session begins by injecting the following system envelope before any module implementation prompt is issued:

```text
You are an expert systems programmer building a 2-player turn-based Connect 4 network game in Python 3.
You MUST adhere strictly to the following architectural and protocol constraints:

1. NETWORKING & ARCHITECTURAL BOUNDARIES:
   - Use ONLY Python standard library modules: `socket`, `struct`, `json`, `argparse`, `selectors` / `select`, `logging`, `sys`.
   - Third-party frameworks (WebSockets, asyncio-http, Twisted, Socket.IO) are STRICTLY FORBIDDEN.
   - The server must bind to all network interfaces ("0.0.0.0") on the specified port.
   - The client must parse arguments via `argparse` supporting `-i <hostname>` and `-p <port>`.

2. CUSTOM PROTOCOL FRAMING (C4P):
   - Transport is raw stream-oriented TCP.
   - Every message MUST begin with a 4-byte header consisting of an unsigned 32-bit integer in
     Network Byte Order (Big-Endian), packed via `struct.pack("!I", len(payload_bytes))`.
   - Never assume `sock.recv(1024)` returns a complete message. You MUST implement an explicit
     accumulation loop `recv_exact(sock, n)` to handle TCP stream chunking, fragmentation, and coalescing.
   - Payloads MUST be UTF-8 encoded JSON matching the exact schema catalog in docs/protocol_blueprint.md.

3. SOCKET LIFECYCLE & RESILIENCE:
   - Implement the TCP 0-byte EOF rule: `if not chunk: return None`. Prevent infinite loops upon remote close.
   - Catch `ConnectionResetError`, `BrokenPipeError`, and `ConnectionAbortedError` on all socket read/write calls.
   - Cleanly handle abrupt disconnects by declaring an immediate forfeit victory for the surviving opponent.
```

---

## 3. Production Prompts & Module Task Specifications

### Prompt 1: Deterministic Network Framing Engine
* **Target File:** `network_transport.py`
* **Governing Specification:** `docs/protocol_blueprint.md` (Sections 1 & 3)

```text
Context:
We are implementing the transport layer for our Connect 4 game according to docs/protocol_blueprint.md.

Task:
Write a clean, modular Python module named `network_transport.py` containing the following functions:
1. `recv_exact(sock: socket.socket, n_bytes: int) -> bytes | None`:
   - Accumulate bytes in a bytearray until exactly `n_bytes` are received.
   - If `sock.recv()` returns `b""`, log the event, return `None` (detecting TCP EOF), and exit immediately.
   - Catch `ConnectionResetError`, `BrokenPipeError`, and `ConnectionAbortedError`, log a warning, and return `None`.
2. `receive_c4p_message(sock: socket.socket) -> dict | None`:
   - Call `recv_exact(sock, 4)` to read the big-endian header.
   - Unpack length $N$ using `struct.unpack("!I", header)[0]`.
   - Validate that $N \le 65536$ (prevent buffer exhaustion). If exceeded, log an error and return `None`.
   - Call `recv_exact(sock, N)` to obtain the exact payload bytes.
   - Decode payload as UTF-8, deserialize via `json.loads()`, and return the dictionary. Return `None` on failure.
3. `send_c4p_message(sock: socket.socket, message_dict: dict) -> bool`:
   - Serialize `message_dict` to a UTF-8 JSON byte string.
   - Prepend a 4-byte big-endian length prefix via `struct.pack("!I", len(payload))`.
   - Dispatch the complete buffer using `sock.sendall()`.
   - Catch `BrokenPipeError` and `ConnectionResetError`, returning `False` on drop and `True` on success.

Requirements:
Include full type hints, PEP 8 styling, and comprehensive docstrings explaining TCP stream accumulation.
```

---

### Prompt 2: Connect 4 Engine & State Machine Validation
* **Target File:** `game_engine.py`
* **Governing Specification:** `docs/fsm_specification.md` (Sections 2 & 3)

```text
Context:
We are implementing the Connect 4 core game logic constrained by docs/fsm_specification.md.
The board is a 6-row by 7-column grid initialized with " " (empty spaces).

Task:
Write a Python class `Connect4Engine` with the following interface:
1. `__init__(self, p1_name: str, p2_name: str)`:
   - Matrix: 6 rows $\times$ 7 columns filled with " ".
   - Roles: Assign "X" to `p1_name` and "O" to `p2_name`.
   - State: Set `current_turn = p1_name`, `move_count = 0`.
2. `drop_token(self, player_id: str, col: int) -> tuple[bool, str, dict]`:
   - Guard 1 (Turn Check): If `player_id != self.current_turn`, return `(False, "ERR_OUT_OF_TURN", {})`.
   - Guard 2 (Range Check): If `col < 0` or `col > 6`, return `(False, "ERR_INVALID_COLUMN", {})`.
   - Guard 3 (Full Column Check): If `self.board[0][col] != " "`, return `(False, "ERR_COLUMN_FULL", {})`.
   - Apply Gravity: Find the lowest unoccupied row `r` from 5 down to 0 where `self.board[r][col] == " "`. Place the player's token. Increment `move_count`.
   - Check Win: Check for 4 matching consecutive tokens along horizontal, vertical, and both diagonal directions. If found, return `(True, "WIN", {"winning_coords": [...], "row": r, "col": col})`.
   - Check Draw: If `move_count == 42` and no win, return `(True, "DRAW", {"row": r, "col": col})`.
   - Continue: If game continues, toggle `current_turn` to the opponent and return `(True, "CONTINUE", {"row": r, "col": col})`.
3. `render_ascii(self) -> str`:
   - Return an ASCII rendering of the grid showing column headers `0 1 2 3 4 5 6` and cell dividers.

Requirements:
State must remain completely unaltered if any validation guard returns `False`.
```

---

### Prompt 3: Server Concurrency & Stateful Disconnect Recovery
* **Target File:** `server.py`
* **Governing Specification:** `docs/fsm_specification.md` (Section 4)

```text
Context:
Implement the server state machine using Python standard library `selectors` for single-threaded non-blocking I/O.

Task:
Write the client disconnection and forfeit handling routine:
1. When `receive_c4p_message(sock)` returns `None` for a registered client socket:
   - Identify the disconnected client handle (`player_id`).
   - If the disconnect occurs during `WAITING_FOR_PLAYERS`:
     - Clear the occupied player slot, unregister socket from selector, close socket, and return lobby to `LOBBY_EMPTY`.
   - If the disconnect occurs during an active match (`PLAYER_TURN` or `EVALUATE_MOVE`):
     - Identify the remaining connected opponent.
     - Build a `GAME_OVER` payload matching Section 2.2 of docs/protocol_blueprint.md:
       `{"outcome": "FORFEIT", "winner_id": opponent_id, "reason": "OPPONENT_DISCONNECTED", ...}`.
     - Send the message to the surviving player socket via `send_c4p_message()`.
     - Close both client sockets defensively using `try/except OSError:`.
     - Reset the server state engine to `WAITING_FOR_PLAYERS`, preserving the listening socket so subsequent matches can run without restarting the daemon process.
```

---

## 4. Verification & Output Audit Checklist

Before accepting any code generated by an AI assistant into the Git repository, execute this mandatory audit checklist:

- [ ] **No Unbuffered Reads:** Confirm that no module contains raw `sock.recv(1024)` calls directly parsed by `json.loads()`.
- [ ] **Endianness Verification:** Confirm that all binary headers explicitly utilize the `!I` format character (Network Byte Order / Big-Endian) in `struct.pack()` and `struct.unpack()`.
- [ ] **EOF Loop Protection:** Verify that every read loop tests `if not chunk:` to prevent infinite loops consuming 100% CPU on CML nodes.
- [ ] **Host Binding:** Verify that `server.py` binds to `0.0.0.0` (all interfaces) rather than `localhost` or `127.0.0.1`.
- [ ] **CLI Argument Conformance:** Verify that `client.py` strictly adheres to required `-i <hostname>` and `-p <port>` flags via `argparse`.
- [ ] **Schema Compliance:** Confirm that all JSON keys (`msg_type`, `player_id`, `payload`, `timestamp`) exactly match the schemas defined in `docs/protocol_blueprint.md`.
