# CS 457 Project Statement of Work (SOW) & Protocol Specification Template

**Student Name:** Mitchel Mata  
**Date:** 9/20/2026 
**Course:** CS 457 - Computer Networks  
**Target Server Domain:** `server.mata.edu

---

## 1. Game Selection & Scope (Sprint 0)

> Planning is going to be an iterative process through the sprints so you don't have to have all the details now. Focus on big overview concepts. You will be updating the SOW as we plan.
> You have a lot of freedom to choose a game. There are a couple caveats.  

> - It must run in the console. The lab nodes won't be able to handle extensive graphics.
> - It has to be self-contained. You can use a internet-connector to download you code, but because the architecture must run 5 nodes you won't be able to run 
> - You are encouraged to use python, but I'm not going to make it a strict requirement. The instructor and TA's ability to help with C or Rust, etc will be diminished in other languages.

### 1.1 Game Overview
- **Chosen Game:** Connect 4
- **Player Capacity:** 2 Players (Simulated via 2 CML Client nodes)
- **Game Summary:** Connect 4 is a classic turn-based connection game played on a standard vertical 6x7 board. Two networked clients connect to a central server and take turns dropping tokens into columns. The tokens drop to the lowest unoccupied slot in the selected column. The game will run strictly in the console using ASCII rendering so it stays lightweight and doesn't run into rendering issues on CML nodes.

### 1.2 Core Game Rules & Win/Draw Conditions
- **Turn Mechanics:** The server is authoritative and tracks whose turn it is. The first client to connect gets assigned Player 1 (`X`) and takes the first turn. The second client to connect gets Player 2 (`O`). The server only accepts inputs from the active player's socket connection—any input sent out of turn is ignored or returned with a turn error. On their turn, a player picks a column index (0–6). If the column has space, the move is placed and turn control flips to the other player.
- **Victory Condition:** A player wins as soon as they get 4 matching tokens in an unbroken line:
  - **Horizontal:** 4 in the same row.
  - **Vertical:** 4 in the same column.
  - **Diagonal:** 4 along either an ascending or descending slope.
- **Victory Condition:** A player wins as soon as they get 4 matching tokens in an unbroken line:
- **Draw/Tie Condition:** A draw happens if all 42 slots on the 6x7 board fill up without either player getting 4 in a row. The server checks if the top row across all columns is completely occupied; if so and there's no winner, it broadcasts a `GAME_OVER` draw state and shuts down the match cleanly.
---

## 2. Application-Layer Messaging Protocol Blueprint (Sprint 1 Deliverable)

### 2.1 Message Transport & Serialization Format
- **Transport Protocol:** TCP
- **Serialization Format:** JSON
- **Framing Mechanism:** Newline-delimited (`\n`) JSON strings. This keeps message boundaries simple and reliable over TCP byte streams without dealing with complex binary packing.

### 2.2 Message Schema Definitions

#### Message Types:
1. `CONNECT` (Client -> Server): Request to join the game session.
2. `LOBBY_WAIT` (Server -> Client): Informs Client 1 they are waiting for Client 2.
3. `GAME_START` (Server -> Clients): Notifies both clients that the match is starting and assigns roles (`Player 1` vs `Player 2`).
4. `MOVE` (Client -> Server): Active client sends their chosen column (0–6).
5. `STATE_UPDATE` (Server -> Clients): Server broadcasts the updated 6x7 board state and indicates whose turn is next.
6. `GAME_OVER` (Server -> Clients): Broadcasts the game result (winner or draw) and final board state.
7. `ERROR` (Server -> Client): Sent to a client if they make an invalid move (column full, out of bounds, or moving out of turn).

#### Example JSON Protocol Schema:
```json
{
  "msg_type": "MOVE",
  "player_id": "Player_1",
  "payload": {
    "col": 3
  },
  "timestamp": 1727000000
}
```

---

### 2.3 Game State Machine (FSM) Design (Sprint 1 Deliverable)
- **State Transitions:** Detail state flow: `INIT` -> `WAITING_FOR_PLAYERS` -> `PLAYER_TURN` -> `EVALUATE_MOVE` -> `CHECK_WIN_DRAW` -> `GAME_OVER` -> `CLEANUP`.

---

## 3. Game Behavior & Server Concurrency Architecture (Sprint 2 Deliverable)

### 3.1 Server Concurrency Strategy
- **Architecture Choice:** [Multi-Threading (`threading.Thread`) OR Non-blocking I/O multiplexing (`select.select` / `selectors`)]
- **Synchronization Logic:** Explain how shared game state and client list are thread-safe (e.g. `threading.Lock`) to prevent race conditions during turn processing.

### 3.2 State & Score Synchronization Across Clients
- **Turn Enforcement:** Detail how the server validates active player ID before processing moves and broadcasts updated turn notifications to all clients.
- **Score & Board Synchronization:** Describe how state broadcasts keep client screens synchronized in real time.

---

## 4. Coding & AI Implementation Plan (Sprint 3)

- **Permitted AI Tools:** [e.g., GitHub Copilot, ChatGPT, Claude]
- **AI Prompting & Constraint Strategy:** Explain how you will constrain AI models to generate code (in Python or your chosen language) that adheres strictly to the protocol blueprint and FSM designed in Sprints 1 & 2.
- **Implementation Risk Management:** Detail your plan to leverage past programming experience and manage time to ensure code completion on schedule.

---

## 5. CML Multi-Subnet Topology & Wireshark Deployment Plan (Sprint 4 & 5 Deliverable)

> For now you can use the topology below. We may update this when we get to defining subnets.

### 5.1 Subnet & Router Design
- **Subnet A (Client 1):** `192.168.10.0/24` (Interface `Gi0/1` on Router R1)
- **Subnet B (Client 2):** `192.168.11.0/24` (Interface `Gi0/2` on Router R1)
- **Subnet C (Game Server):** `192.168.20.0/24` (Interface `Gi0/1` on Router R2)
- **Router Backbone:** `10.0.0.0/30` (Interface `Gi0/0` on R1 <-> `Gi0/0` on R2)

### 5.2 DHCP Pools & DNS Configuration Plan
- **Router R1 DHCP Pool 1 (`CLIENT1_POOL`):** Leases `192.168.10.10` - `192.168.10.50`, gateway `192.168.10.1`, DNS `10.0.0.2`.
- **Router R1 DHCP Pool 2 (`CLIENT2_POOL`):** Leases `192.168.11.10` - `192.168.11.50`, gateway `192.168.11.1`, DNS `10.0.0.2`.
- **Router R2 Authoritative DNS:** Configured with `ip dns server` and static host mapping `server.[yourlastname].edu` -> `192.168.20.100`.

### 5.3 Deployment Strategy & Wireshark Trace Capture
- **CML Deployment Strategy:** Deploy `server.py` onto Subnet C node (`192.168.20.100`) behind Router R2, and `client.py` onto Subnet A and Subnet B nodes behind Router R1.
- **Cisco Infrastructure Configuration:** Router R1 DHCP pools (`CLIENT1_POOL`, `CLIENT2_POOL`) and Router R2 authoritative DNS (`ip host server.[lastname].edu 192.168.20.100`).
- **Wireshark Trace Capture Plan:** Capture DHCP DORA exchange (`dhcp_negotiation.pcap`) and DNS query/response resolution (`dns_lookup.pcap`).
