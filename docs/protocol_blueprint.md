# Application Protocol Blueprint: Connect 4 Wire Protocol (C4P)
**Version:** 1.0  
**Transport Layer:** Transmission Control Protocol (TCP)  
**Serialization:** Structured UTF-8 JSON  
**Framing Mechanism:** 4-Byte Big-Endian Length-Prefixed Framing  
**Target Topology:** Cisco Modeling Labs (CML) Multi-Subnet Routed Network  

---

## 1. Transport-Layer Packet Framing & Stream Boundary Specification

### 1.1 The TCP Byte-Stream Problem
TCP is an unstructured byte-stream transport protocol (RFC 793). It guarantees ordered, reliable delivery, but provides **no intrinsic application-layer message boundaries**. When reading and writing raw sockets:
1. **Packet Coalescing (Aggregation):** Multiple messages transmitted back-to-back can be merged into a single network read chunk due to OS socket buffering or Nagle's algorithm.
2. **Packet Fragmentation:** A single application message can be fragmented across multiple TCP packets due to network MTU/MSS constraints, socket buffer sizing, or intermediate router queuing across the CML backbone ($R_1 \leftrightarrow R_2$).

A naive read pattern like `sock.recv(1024)` assuming one `send()` equals one `recv()` will cause protocol corruption when running across routed subnets.

### 1.2 Framing Mechanism: 4-Byte Binary Length Prefix (Network Byte Order)
To ensure deterministic message extraction across the continuous TCP byte stream without delimiter collisions:
- Every message is prepended with a **4-byte fixed-width binary header**.
- The header encodes an unsigned 32-bit integer (`uint32`) formatted in **Network Byte Order (Big-Endian)**.
- In Python, this is packed using `struct.pack("!I", length)`.
- The integer represents the **exact byte length ($N$)** of the UTF-8 encoded JSON payload that immediately follows.
- The maximum supported payload length is bounded to $64\text{ KiB}$ ($65,536\text{ bytes}$) for memory safety, preventing Denial-of-Service or buffer exhaustion.

```text
+-----------------------------------+---------------------------------------------+
|    4-Byte Header (Big-Endian)     |          N-Byte Payload (UTF-8 JSON)        |
|    Length = N (e.g., 0x0000004D)  |  {"msg_type": "CONNECT", ... }              |
+-----------------------------------+---------------------------------------------+
|<----------- 4 Bytes ------------->|<----------------- N Bytes ----------------->|
```
### 1.3 Wire Stream Example (Continuous Stream with Coalescing)
Consider Client 1 (`Alice`) sending a `CONNECT` message immediately followed by a `MOVE` (dropping a piece into column 3) in a single burst:

- **Message 1 (`CONNECT`):** Payload length = 77 bytes (`0x0000004D`)
- **Message 2 (`MOVE`):** Payload length = 85 bytes (`0x00000055`)

#### Continuous Raw Hexadecimal Stream on the Wire:
```hex
00 00 00 4D 7B 22 6D 73 67 5F 74 79 70 65 22 3A 22 43 4F 4E 4E 45 43 54 22 2C 22 70 6C 61 79 65 72 5F 69 64 22 3A 22 41 6C 69 63 65 22 2C 22 70 61 79 6C 6F 61 64 22 3A 7B 7D 2C 22 74 69 6D 65 73 74 61 6D 70 22 3A 31 37 32 37 30 30 30 30 30 30 7D 00 00 00 55 7B 22 6D 73 67 5F 74 79 70 65 22 3A 22 4D 4F 56 45 22 2C 22 70 6C 61 79 65 72 5F 69 64 22 3A 22 41 6C 69 63 65 22 2C 22 70 61 79 6C 6F 61 64 22 3A 7B 22 63 6F 6C 22 3A 33 7D 2C 22 74 69 6D 65 73 74 61 6D 70 22 3A 31 37 32 37 30 30 30 30 30 35 7D
```

#### Annotated Breakdown:
```text
[Header 1]: 00 00 00 4D                          (4 bytes: length = 77)
[Body 1]:   {"msg_type":"CONNECT","player_id":"Alice","payload":{},"timestamp":1727000000}
[Header 2]: 00 00 00 55                          (4 bytes: length = 85)
[Body 2]:   {"msg_type":"MOVE","player_id":"Alice","payload":{"col":3},"timestamp":1727000005}
```

### 1.4 Framing Receiver Extraction Algorithm
The receiver implements an exact-byte accumulation loop (`recv_exact`):
1. **Header Extraction:** Read from the socket until exactly 4 bytes are accumulated in the buffer.
2. **Length Unpacking:** Decode the integer length $N$ using `struct.unpack("!I", header)[0]`.
3. **Payload Extraction:** Read from the socket until exactly $N$ payload bytes are accumulated.
4. **Deserialization:** Decode the accumulated bytes as UTF-8 and pass them to `json.loads()`.

---

## 2. Structured Application Message Types (C4P)

All messages use a uniform JSON envelope structure:
```json
{
  "msg_type": "STRING",
  "player_id": "STRING",
  "payload": {},
  "timestamp": 1727000000
}
```

### 2.1 Message Catalog

| Message Type | Direction | Purpose |
| :--- | :--- | :--- |
| `CONNECT` | Client $\rightarrow$ Server | Client requests entry into the game lobby with a player alias. |
| `LOBBY_WAIT` | Server $\rightarrow$ Client | Informs Client 1 that the server is waiting for Client 2 to connect. |
| `GAME_START` | Server $\rightarrow$ Clients | Notifies both clients of match start, role assignment (`X` or `O`), and starting turn. |
| `MOVE` | Client $\rightarrow$ Server | Active player submits a target column index ($0 \le \text{col} \le 6$). |
| `STATE_UPDATE` | Server $\rightarrow$ Clients | Broadcasts updated $6 \times 7$ board matrix, placed token coordinates, and next turn. |
| `ERROR` | Server $\rightarrow$ Client | Informs client of out-of-turn moves, full columns, or invalid inputs. |
| `DISCONNECT` | Client $\rightarrow$ Server | Graceful notification that a client is quitting. |
| `GAME_OVER` | Server $\rightarrow$ Clients | Broadcasts terminal outcome (`WIN`, `DRAW`, `FORFEIT`), winning line, and final board. |

---

### 2.2 Detailed Message Schemas

#### 1. `CONNECT` (Client $\rightarrow$ Server)
Initiated by a client immediately after opening the TCP connection.
- `msg_type` (string): `"CONNECT"`
- `player_id` (string): Alphanumeric client nickname (1–16 chars)
- `payload.client_version` (string): Protocol version identifier
- `timestamp` (integer): Unix epoch timestamp in seconds
```json
{
  "msg_type": "CONNECT",
  "player_id": "Alice",
  "payload": {
    "client_version": "1.0"
  },
  "timestamp": 1727000000
}
```

#### 2. `LOBBY_WAIT` (Server $\rightarrow$ Client)
Dispatched to Player 1 while waiting for Player 2 to establish a connection.
- `msg_type` (string): `"LOBBY_WAIT"`
- `player_id` (string): `"SERVER"`
- `payload.message` (string): Status notice string
- `payload.assigned_role` (string): Reserved token symbol (`"X"`)
- `timestamp` (integer): Unix epoch timestamp in seconds
```json
{
  "msg_type": "LOBBY_WAIT",
  "player_id": "SERVER",
  "payload": {
    "message": "Waiting for Player 2 to connect...",
    "assigned_role": "X"
  },
  "timestamp": 1727000001
}
```

#### 3. `GAME_START` (Server $\rightarrow$ Clients)
Broadcast when both players are connected and ready.
- `msg_type` (string): `"GAME_START"`
- `player_id` (string): `"SERVER"`
- `payload.your_role` (string): Assigned role (`"X"` for Player 1, `"O"` for Player 2)
- `payload.opponent_id` (string): Username of the opponent
- `payload.starting_player` (string): Username of the player who moves first
- `payload.rows` (integer): Number of board rows (`6`)
- `payload.cols` (integer): Number of board columns (`7`)
- `payload.board` (array of array of strings): Initial empty $6 \times 7$ grid
- `timestamp` (integer): Unix epoch timestamp in seconds
```json
{
  "msg_type": "GAME_START",
  "player_id": "SERVER",
  "payload": {
    "your_role": "X",
    "opponent_id": "Bob",
    "starting_player": "Alice",
    "rows": 6,
    "cols": 7,
    "board": [
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "]
    ]
  },
  "timestamp": 1727000005
}
```

#### 4. `MOVE` (Client $\rightarrow$ Server)
Submitted by the active player to drop a token.
- `msg_type` (string): `"MOVE"`
- `player_id` (string): Username of submitting client
- `payload.col` (integer): Target column index ($0 \le \text{col} \le 6$)
- `timestamp` (integer): Unix epoch timestamp in seconds
```json
{
  "msg_type": "MOVE",
  "player_id": "Alice",
  "payload": {
    "col": 3
  },
  "timestamp": 1727000010
}
```

#### 5. `STATE_UPDATE` (Server $\rightarrow$ Clients)
Broadcast after a successful move.
- `msg_type` (string): `"STATE_UPDATE"`
- `player_id` (string): `"SERVER"`
- `payload.last_move` (object): Metadata of the move just executed
  - `player` (string): Player alias
  - `role` (string): `"X"` or `"O"`
  - `row` (integer): Lowest unoccupied row where token landed due to gravity ($0 \le \text{row} \le 5$)
  - `col` (integer): Column index ($0 \le \text{col} \le 6$)
- `payload.board` (array of array of strings): Updated full $6 \times 7$ matrix
- `payload.current_turn` (string): Username of the player whose turn is next
- `timestamp` (integer): Unix epoch timestamp in seconds
```json
{
  "msg_type": "STATE_UPDATE",
  "player_id": "SERVER",
  "payload": {
    "last_move": {
      "player": "Alice",
      "role": "X",
      "row": 5,
      "col": 3
    },
    "board": [
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", "X", " ", " ", " "]
    ],
    "current_turn": "Bob"
  },
  "timestamp": 1727000011
}
```

#### 6. `ERROR` (Server $\rightarrow$ Client)
Unicast to an offending client when an invalid request is detected.
- `msg_type` (string): `"ERROR"`
- `player_id` (string): `"SERVER"`
- `payload.error_code` (string): Machine-readable failure token
- `payload.error_message` (string): Human-readable error description
- `payload.rejected_action` (string): The message type that caused the rejection
- `timestamp` (integer): Unix epoch timestamp in seconds
```json
{
  "msg_type": "ERROR",
  "player_id": "SERVER",
  "payload": {
    "error_code": "ERR_COLUMN_FULL",
    "error_message": "Column 3 is already full. Choose another column.",
    "rejected_action": "MOVE"
  },
  "timestamp": 1727000015
}
```
*Standard Error Codes:*
- `ERR_NOT_IN_LOBBY`: Action attempted before lobby registration.
- `ERR_OUT_OF_TURN`: Client attempted to move when `current_turn != player_id`.
- `ERR_INVALID_COLUMN`: Column index is out of bounds ($\text{col} < 0$ or $\text{col} > 6$).
- `ERR_COLUMN_FULL`: Target column top slot (`row 0`) is already occupied.
- `ERR_MALFORMED_JSON`: Incoming packet failed JSON deserialization or schema validation.

#### 7. `DISCONNECT` (Client $\rightarrow$ Server)
Graceful notification of departure.
- `msg_type` (string): `"DISCONNECT"`
- `player_id` (string): Alias of disconnecting player
- `payload.reason` (string): Reason code (e.g., `"USER_QUIT"`)
- `timestamp` (integer): Unix epoch timestamp in seconds
```json
{
  "msg_type": "DISCONNECT",
  "player_id": "Alice",
  "payload": {
    "reason": "USER_QUIT"
  },
  "timestamp": 1727000050
}
```

#### 8. `GAME_OVER` (Server $\rightarrow$ Clients)
Broadcast upon a terminal condition (Win, Draw, or Forfeit).
- `msg_type` (string): `"GAME_OVER"`
- `player_id` (string): `"SERVER"`
- `payload.outcome` (string): `"WIN"`, `"DRAW"`, or `"FORFEIT"`
- `payload.winner_id` (string or null): Username of winner, or null if draw
- `payload.winning_role` (string or null): Winning token (`"X"` or `"O"`), or null if draw
- `payload.reason` (string): `"CONNECT_FOUR"`, `"BOARD_FULL"`, or `"OPPONENT_DISCONNECTED"`
- `payload.winning_coordinates` (array of coordinate pairs): Coordinates forming the 4-in-a-row (empty on draw/forfeit)
- `payload.final_board` (array of array of strings): Terminal state of the board
- `timestamp` (integer): Unix epoch timestamp in seconds
```json
{
  "msg_type": "GAME_OVER",
  "player_id": "SERVER",
  "payload": {
    "outcome": "WIN",
    "winner_id": "Alice",
    "winning_role": "X",
    "reason": "CONNECT_FOUR",
    "winning_coordinates": [[5, 3], [4, 3], [3, 3], [2, 3]],
    "final_board": [
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", " ", " ", " ", " "],
      [" ", " ", " ", "X", " ", " ", " "],
      [" ", " ", " ", "X", "O", " ", " "],
      [" ", " ", "O", "X", "O", " ", " "],
      ["O", " ", "O", "X", "X", " ", " "]
    ]
  },
  "timestamp": 1727000060
}
```

---

## 3. Connection Termination & Socket Lifecycle Architecture

### 3.1 Graceful Application Disconnect vs. Transport Teardown
1. **Application-Layer Disconnect (`DISCONNECT` message):** When a player exits gracefully (e.g., entering `quit` in the CLI), the client transmits an application-layer `DISCONNECT` message. The server intercepts this message, awards an immediate forfeit victory to the remaining client, broadcasts `GAME_OVER`, and cleanly closes the sockets.
2. **Transport-Layer Teardown (TCP FIN):** When a process calls `sock.close()` or exits normally, the host operating system sends a TCP `FIN` packet to initiate the standard 4-way TCP connection teardown handshake (`FIN` $\rightarrow$ `ACK` $\rightarrow$ `FIN` $\rightarrow$ `ACK`).

### 3.2 The POSIX TCP EOF (0-Byte) Rule
When a remote endpoint shuts down its write connection cleanly via TCP FIN, calling `sock.recv()` does **not** raise an exception. Instead, it returns an empty byte string (`b""` / 0 bytes), signaling End-of-File (EOF).
- **The Infinite Loop Hazard:** If a socket receive loop does not verify `if not chunk:`, subsequent calls to `recv()` will immediately return `b""` without blocking. This causes a tight infinite loop that consumes 100% CPU on the CML node and stalls server concurrency.
- **Enforced Rule:** Any read returning `b""` during header or payload accumulation is treated as an immediate client disconnection.

### 3.3 Abrupt Termination (TCP RST, Link Severance, CML Impairments)
In a CML virtual network, router links can be severed, interfaces disabled, or processes killed abruptly (`SIGKILL` / `kill -9`):
- No TCP FIN handshake is performed.
- Subsequent socket operations raise low-level OS exceptions:
  - `ConnectionResetError` (ECONNRESET): The remote peer sent a TCP RST packet because the connection was forcefully broken.
  - `BrokenPipeError` (EPIPE): The server attempted to call `sock.send()` on a socket whose remote peer is closed.
  - `ConnectionAbortedError`: The connection was aborted by local software or network stack.
  - `TimeoutError`: Socket read timeout expired during severe packet loss or latency.

### 3.4 Transport Engine Implementation Reference (Python 3)
```python
import struct
import json
import logging

logger = logging.getLogger("C4_Transport")

def recv_exact(sock, n_bytes):
    """
    Accumulates exactly n_bytes from a TCP socket.
    Guarantees message boundary extraction across fragmented packets.
    Detects 0-byte EOF and socket drop exceptions.
    """
    buf = bytearray()
    while len(buf) < n_bytes:
        try:
            chunk = sock.recv(n_bytes - len(buf))
            if not chunk:
                # 0-byte read: remote peer closed connection cleanly (TCP FIN / EOF)
                logger.warning("TCP EOF detected (remote socket closed).")
                return None
            buf.extend(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as err:
            logger.error(f"Abrupt connection drop encountered: {err}")
            return None
    return bytes(buf)

def receive_c4p_message(sock):
    """
    Reads a complete length-prefixed C4P message from the wire.
    """
    # 1. Read fixed 4-byte big-endian header
    header_bytes = recv_exact(sock, 4)
    if not header_bytes:
        return None
    
    payload_len = struct.unpack("!I", header_bytes)[0]
    
    # 2. Prevent buffer exhaustion attacks (max 64 KiB)
    if payload_len > 65536:
        logger.error(f"Payload size {payload_len} exceeds maximum allowed boundary.")
        return None
        
    # 3. Read exact payload bytes
    payload_bytes = recv_exact(sock, payload_len)
    if not payload_bytes:
        return None
        
    return json.loads(payload_bytes.decode("utf-8"))

def send_c4p_message(sock, message_dict):
    """
    Packs and transmits a JSON payload preceded by a 4-byte big-endian length prefix.
    """
    try:
        payload_bytes = json.dumps(message_dict).encode("utf-8")
        header = struct.pack("!I", len(payload_bytes))
        sock.sendall(header + payload_bytes)
        return True
    except (BrokenPipeError, ConnectionResetError) as err:
        logger.error(f"Failed to transmit packet: {err}")
        return False
```

