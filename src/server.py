#!/usr/bin/env python3
"""Connect 4 (C4P) server.

Usage:
    python3 server.py -h
    python3 server.py -p 8080

Architecture (docs/concurrency_design.md): one thread, one
selectors.DefaultSelector event loop, non-blocking sockets, a per-connection
receive buffer with an incremental frame decoder, and a per-connection
transmit buffer. Handlers run to completion, so shared game state needs no
locks. Client identity always comes from the socket (conn.slot), never from
the player_id a client claims.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import selectors
import signal
import socket
import struct
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum, auto
from selectors import EVENT_READ, EVENT_WRITE

from game_engine import COLS, ROWS, Connect4Engine
from network_transport import (MAX_FRAME_SIZE, build_message,
                               encode_c4p_message, port_number)

logger = logging.getLogger("C4_Server")

HANDSHAKE_TIMEOUT = 10.0     # seconds to send a valid CONNECT
FLUSH_TIMEOUT = 2.0          # max wait to flush final frames before closing
MAX_TX_BUF = 65536           # peer that stops reading is treated as dead
SELECT_TIMEOUT = 1.0
LISTEN_BACKLOG = 5
ALIAS_RE = re.compile(r"[A-Za-z0-9]{1,16}")


class Phase(Enum):
    """Long-lived FSM phases. Other FSM states run inside one handler call."""

    WAITING_FOR_PLAYERS = auto()
    PLAYER_TURN = auto()
    GAME_OVER = auto()


@dataclass
class Connection:
    """Per-socket state for the event loop."""

    sock: socket.socket
    addr: tuple
    rx_buf: bytearray = field(default_factory=bytearray)
    tx_buf: bytearray = field(default_factory=bytearray)
    alias: str | None = None
    slot: int | None = None            # 0 = Player 1 (X), 1 = Player 2 (O)
    connected_at: float = field(default_factory=time.monotonic)
    closed: bool = False               # guards stale events in one select()
    close_after_flush: bool = False    # close once tx_buf drains
    close_deadline: float = 0.0


@dataclass
class ScoreRecord:
    """Cumulative in-memory record for one alias."""

    wins: int = 0
    losses: int = 0
    draws: int = 0


class Server:
    """Connect 4 server: event loop, lobby, match FSM, scoring."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.sel = selectors.DefaultSelector()
        self.listener: socket.socket | None = None
        self.conns: dict[socket.socket, Connection] = {}
        self.players: list[Connection | None] = [None, None]
        self.engine: Connect4Engine | None = None
        self.scores: dict[str, ScoreRecord] = {}
        self.phase = Phase.WAITING_FOR_PLAYERS
        self.running = False

    # ------------------------------------------------------------------
    # FSM logging helpers
    # ------------------------------------------------------------------
    def set_phase(self, new_phase: Phase) -> None:
        """Move to a new long-lived phase and log the transition."""
        logger.info("FSM: %s -> %s", self.phase.name, new_phase.name)
        self.phase = new_phase

    @staticmethod
    def log_fsm(state: str) -> None:
        """Log a transient FSM state that runs inside a single handler."""
        logger.info("FSM: [%s]", state)

    # ------------------------------------------------------------------
    # Startup / event loop
    # ------------------------------------------------------------------
    def start(self) -> None:
        """INIT: bind 0.0.0.0:PORT, listen, register with the selector."""
        self.log_fsm("INIT")
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("0.0.0.0", self.port))
        self.listener.listen(LISTEN_BACKLOG)
        self.listener.setblocking(False)
        self.sel.register(self.listener, EVENT_READ, data=None)
        self.running = True
        logger.info("Listening on 0.0.0.0:%d", self.port)
        self.log_fsm("WAITING_FOR_PLAYERS")

    def run(self) -> None:
        """Main event loop. The only blocking call is selector.select()."""
        try:
            while self.running:
                for key, mask in self.sel.select(timeout=SELECT_TIMEOUT):
                    if key.data is None:
                        self.accept_ready()
                        continue
                    conn = key.data
                    if conn.closed:
                        continue          # stale event from earlier in batch
                    if mask & EVENT_READ:
                        self.read_ready(conn)
                    if mask & EVENT_WRITE and not conn.closed:
                        self.try_flush(conn)
                self.sweep_timeouts()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Close every socket and the selector."""
        logger.info("Shutting down.")
        for conn in list(self.conns.values()):
            self.close_connection(conn)
        if self.listener is not None:
            try:
                self.sel.unregister(self.listener)
            except (KeyError, ValueError):
                pass
            self.listener.close()
        self.sel.close()

    def stop(self, *_args) -> None:
        """Signal handler: ask the loop to exit."""
        self.running = False

    # ------------------------------------------------------------------
    # Accept / read / write
    # ------------------------------------------------------------------
    def accept_ready(self) -> None:
        """Accept every pending connection (until BlockingIOError)."""
        while True:
            try:
                sock, addr = self.listener.accept()
            except BlockingIOError:
                return
            except OSError as err:
                logger.warning("accept() failed: %s", err)
                return
            sock.setblocking(False)
            self.enable_keepalive(sock)
            conn = Connection(sock=sock, addr=addr)
            self.conns[sock] = conn
            self.sel.register(sock, EVENT_READ, data=conn)
            logger.info("Accepted connection from %s:%d", *addr)

    @staticmethod
    def enable_keepalive(sock: socket.socket) -> None:
        """Detect severed links (no FIN/RST) via TCP keepalive."""
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10),
                            ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):      # Linux; absent on some platforms
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name),
                                value)

    def read_ready(self, conn: Connection) -> None:
        """Read bytes and extract every complete C4P frame."""
        try:
            chunk = conn.sock.recv(4096)
        except BlockingIOError:
            return                         # spurious wakeup
        except OSError:                    # RST, abort, timeout
            self.handle_disconnect(conn, "RST")
            return
        if not chunk:                      # POSIX EOF rule
            self.handle_disconnect(conn, "EOF")
            return
        if conn.close_after_flush:
            return                         # draining: ignore further input

        conn.rx_buf.extend(chunk)
        while not conn.closed:
            if len(conn.rx_buf) < 4:
                break                      # header incomplete
            (length,) = struct.unpack("!I", conn.rx_buf[:4])
            if length > MAX_FRAME_SIZE:
                logger.error("Oversized frame (%d bytes) from %s",
                             length, conn.addr)
                self.handle_disconnect(conn, "PROTOCOL_VIOLATION")
                return
            if len(conn.rx_buf) < 4 + length:
                break                      # payload incomplete
            payload = bytes(conn.rx_buf[4:4 + length])
            del conn.rx_buf[:4 + length]
            self.dispatch(conn, payload)

    def enqueue(self, conn: Connection, frame: bytes) -> None:
        """Queue a frame for a connection and try an immediate send."""
        if conn.closed:
            return
        conn.tx_buf.extend(frame)
        if len(conn.tx_buf) > MAX_TX_BUF:
            self.handle_disconnect(conn, "TX_OVERFLOW")
            return
        self.try_flush(conn)

    def try_flush(self, conn: Connection) -> None:
        """Send as much of tx_buf as the kernel accepts without blocking."""
        if conn.closed:
            return
        if conn.tx_buf:
            try:
                sent = conn.sock.send(conn.tx_buf)
                del conn.tx_buf[:sent]
            except BlockingIOError:
                pass                       # retry on EVENT_WRITE
            except OSError:
                self.handle_disconnect(conn, "SEND_FAIL")
                return
        if not conn.tx_buf and conn.close_after_flush:
            self.close_connection(conn)
            return
        events = EVENT_READ | (EVENT_WRITE if conn.tx_buf else 0)
        try:
            self.sel.modify(conn.sock, events, conn)
        except (KeyError, ValueError, OSError):
            pass

    def sweep_timeouts(self) -> None:
        """Close silent handshakes and connections that never drained."""
        now = time.monotonic()
        for conn in list(self.conns.values()):
            if conn.closed:
                continue
            if conn.close_after_flush:
                if now > conn.close_deadline:
                    self.close_connection(conn)
            elif conn.alias is None \
                    and now - conn.connected_at > HANDSHAKE_TIMEOUT:
                logger.info("Handshake timeout for %s", conn.addr)
                self.close_connection(conn)

    # ------------------------------------------------------------------
    # Dispatch and message helpers
    # ------------------------------------------------------------------
    def dispatch(self, conn: Connection, payload: bytes) -> None:
        """Decode JSON and route by msg_type."""
        try:
            msg = json.loads(payload.decode("utf-8"))
            if not isinstance(msg, dict) \
                    or not isinstance(msg.get("msg_type"), str):
                raise ValueError("bad envelope")
        except ValueError:                 # includes UnicodeDecodeError
            self.send_error(conn, "ERR_MALFORMED_JSON",
                            "Message is not valid C4P JSON.", "UNKNOWN")
            return

        msg_type = msg["msg_type"]
        logger.info("RX %-10s from %s", msg_type, conn.alias or conn.addr)
        if msg_type == "CONNECT":
            self.handle_connect(conn, msg)
        elif msg_type == "MOVE":
            self.handle_move(conn, msg)
        elif msg_type == "DISCONNECT":
            self.handle_disconnect(conn, "USER_QUIT")
        else:
            self.send_error(conn, "ERR_MALFORMED_JSON",
                            f"Unknown message type '{msg_type}'.", msg_type)

    def send_error(self, conn: Connection, code: str, text: str,
                   rejected: str) -> None:
        """Unicast an ERROR message to one client."""
        logger.info("TX ERROR      to %s: %s", conn.alias or conn.addr, code)
        frame = encode_c4p_message(build_message("ERROR", "SERVER", {
            "error_code": code,
            "error_message": text,
            "rejected_action": rejected,
        }))
        self.enqueue(conn, frame)

    def reject_and_close(self, conn: Connection, code: str, text: str,
                         rejected: str) -> None:
        """Send an ERROR, flush it, then close the connection."""
        conn.close_after_flush = True
        conn.close_deadline = time.monotonic() + FLUSH_TIMEOUT
        self.send_error(conn, code, text, rejected)

    def scores_payload(self) -> dict:
        """Current records for the two match participants."""
        names = [self.engine.p1, self.engine.p2] if self.engine else []
        return {n: asdict(self.scores.setdefault(n, ScoreRecord()))
                for n in names}

    # ------------------------------------------------------------------
    # FSM handlers
    # ------------------------------------------------------------------
    def handle_connect(self, conn: Connection, msg: dict) -> None:
        """CS-4 registration: validate alias, assign slot and role."""
        if conn.alias is not None:
            self.send_error(conn, "ERR_MALFORMED_JSON",
                            "Already registered.", "CONNECT")
            return
        alias = msg.get("player_id")
        if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
            self.reject_and_close(
                conn, "ERR_MALFORMED_JSON",
                "Alias must be 1-16 letters or digits.", "CONNECT")
            return

        slot = next((i for i, p in enumerate(self.players) if p is None),
                    None)
        if self.phase != Phase.WAITING_FOR_PLAYERS or slot is None:
            self.reject_and_close(conn, "ERR_LOBBY_FULL",
                                  "A match is already in progress.",
                                  "CONNECT")
            return
        taken = {p.alias.casefold() for p in self.players if p} | {"server"}
        if alias.casefold() in taken:
            self.reject_and_close(conn, "ERR_ALIAS_TAKEN",
                                  f"Alias '{alias}' is already in use.",
                                  "CONNECT")
            return

        conn.alias, conn.slot = alias, slot
        self.players[slot] = conn
        role = "X" if slot == 0 else "O"
        logger.info("Registered %s as Player %d (%s)", alias, slot + 1, role)

        if all(self.players):
            self.start_match()
        else:
            logger.info("TX LOBBY_WAIT to %s", alias)
            self.enqueue(conn, encode_c4p_message(build_message(
                "LOBBY_WAIT", "SERVER", {
                    "message": "Waiting for Player 2 to connect...",
                    "assigned_role": role,
                })))

    def start_match(self) -> None:
        """GAME_START: create the engine and send per-player GAME_START."""
        self.log_fsm("GAME_START")
        p1, p2 = self.players
        self.engine = Connect4Engine(p1.alias, p2.alias)
        self.set_phase(Phase.PLAYER_TURN)
        scores = self.scores_payload()
        for me, other in ((p1, p2), (p2, p1)):
            if self.phase != Phase.PLAYER_TURN:
                break                      # a send failure ended the match
            logger.info("TX GAME_START to %s", me.alias)
            self.enqueue(me, encode_c4p_message(build_message(
                "GAME_START", "SERVER", {
                    "your_role": self.engine.roles[me.alias],
                    "opponent_id": other.alias,
                    "starting_player": self.engine.current_turn,
                    "rows": ROWS,
                    "cols": COLS,
                    "board": self.engine.board_copy(),
                    "scores": scores,
                })))

    def handle_move(self, conn: Connection, msg: dict) -> None:
        """CS-1 / CS-2: validate, apply, and broadcast a move."""
        if self.phase != Phase.PLAYER_TURN or conn.slot is None \
                or self.engine is None:
            self.send_error(conn, "ERR_NOT_IN_LOBBY",
                            "No active match for this connection.", "MOVE")
            return
        body = msg.get("payload")
        col = body.get("col") if isinstance(body, dict) else None
        if msg.get("player_id") != conn.alias or isinstance(col, bool) \
                or not isinstance(col, int):
            self.send_error(conn, "ERR_MALFORMED_JSON",
                            "MOVE needs your alias and an integer column.",
                            "MOVE")
            return
        if conn.alias != self.engine.current_turn:
            self.send_error(conn, "ERR_OUT_OF_TURN",
                            "It is not your turn.", "MOVE")
            return

        self.log_fsm("EVALUATE_MOVE")
        accepted, status, info = self.engine.drop_token(conn.alias, col)
        if not accepted:
            texts = {
                "ERR_OUT_OF_TURN": "It is not your turn.",
                "ERR_INVALID_COLUMN": f"Column must be 0-{COLS - 1}.",
                "ERR_COLUMN_FULL":
                    f"Column {col} is already full. Choose another column.",
            }
            self.send_error(conn, status, texts[status], "MOVE")
            self.log_fsm("PLAYER_TURN (turn kept)")
            return

        logger.info("Move accepted: %s dropped %s in column %d (row %d)",
                    conn.alias, info["role"], col, info["row"])
        self.log_fsm("CHECK_WIN_DRAW")
        if status == "WIN":
            self.finish_match("WIN", conn.alias, None, "CONNECT_FOUR",
                              info["winning_coords"])
        elif status == "DRAW":
            self.finish_match("DRAW", None, None, "BOARD_FULL", [])
        else:
            self.broadcast_state(conn, info)

    def broadcast_state(self, mover: Connection, info: dict) -> None:
        """CS-2: serialize STATE_UPDATE once, enqueue identical bytes."""
        frame = encode_c4p_message(build_message("STATE_UPDATE", "SERVER", {
            "last_move": {"player": mover.alias, "role": info["role"],
                          "row": info["row"], "col": info["col"]},
            "board": self.engine.board_copy(),
            "current_turn": self.engine.current_turn,
        }))
        for target in list(self.players):          # snapshot
            if self.phase != Phase.PLAYER_TURN:
                break                      # match ended mid-broadcast
            if target is not None:
                logger.info("TX STATE_UPDATE to %s (turn: %s)",
                            target.alias, self.engine.current_turn)
                self.enqueue(target, frame)
        self.log_fsm("PLAYER_TURN")

    def finish_match(self, outcome: str, winner: str | None,
                     leaver: Connection | None, reason: str,
                     coords: list) -> None:
        """CS-3: resolve outcome, update scores, broadcast GAME_OVER."""
        self.log_fsm("GAME_OVER")
        self.set_phase(Phase.GAME_OVER)
        engine = self.engine
        recipients = [p for p in self.players
                      if p is not None and p is not leaver and not p.closed]

        if outcome == "DRAW":
            for name in (engine.p1, engine.p2):
                self.scores.setdefault(name, ScoreRecord()).draws += 1
        else:
            loser = engine.opponent_of(winner)
            self.scores.setdefault(winner, ScoreRecord()).wins += 1
            self.scores.setdefault(loser, ScoreRecord()).losses += 1

        frame = encode_c4p_message(build_message("GAME_OVER", "SERVER", {
            "outcome": outcome,
            "winner_id": winner,
            "winning_role": engine.roles[winner] if winner else None,
            "reason": reason,
            "winning_coordinates": coords,
            "final_board": engine.board_copy(),
            "scores": self.scores_payload(),
        }))
        logger.info("Result: %s winner=%s reason=%s", outcome, winner, reason)

        if leaver is not None:
            self.close_connection(leaver)
        for target in recipients:
            logger.info("TX GAME_OVER to %s", target.alias)
            self.enqueue(target, frame)
        self.cleanup_match()

    def cleanup_match(self) -> None:
        """CS-5: drain and close player sockets, reset to the lobby."""
        self.log_fsm("CLEANUP")
        draining = [p for p in self.players if p is not None and not p.closed]
        self.players = [None, None]
        self.engine = None
        self.set_phase(Phase.WAITING_FOR_PLAYERS)
        for conn in draining:
            conn.slot = None
            conn.close_after_flush = True
            conn.close_deadline = time.monotonic() + FLUSH_TIMEOUT
            self.try_flush(conn)

    def handle_disconnect(self, conn: Connection, reason: str) -> None:
        """Single entry point for every kind of disconnect."""
        if conn.closed:
            return
        logger.info("Disconnect: %s (%s)", conn.alias or conn.addr, reason)
        in_match = (self.phase == Phase.PLAYER_TURN and self.engine
                    and conn.slot is not None and not conn.close_after_flush)
        if not in_match:
            # Handshake, lobby, draining, or already ending: just close.
            self.close_connection(conn)
            return
        winner = self.engine.opponent_of(conn.alias)
        self.finish_match("FORFEIT", winner, conn,
                          "OPPONENT_DISCONNECTED", [])

    def close_connection(self, conn: Connection) -> None:
        """Idempotent teardown of one connection."""
        if conn.closed:
            return
        conn.closed = True
        try:
            self.sel.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        try:
            conn.sock.close()
        except OSError:
            pass
        self.conns.pop(conn.sock, None)
        if conn.slot is not None and self.players[conn.slot] is conn:
            self.players[conn.slot] = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse server CLI arguments (-h, -p)."""
    parser = argparse.ArgumentParser(
        description="Connect 4 (C4P) game server. Binds to 0.0.0.0.",
        epilog="Example: python3 server.py -p 8080")
    parser.add_argument("-p", "--port", type=port_number, required=True,
                        help="listening TCP port (1024-65535)")
    return parser.parse_args(argv)


def main() -> int:
    """Entry point."""
    args = parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    server = Server(args.port)
    signal.signal(signal.SIGTERM, server.stop)
    try:
        server.start()
    except OSError as err:
        print(f"[ERROR] Could not bind to port {args.port}: {err}")
        return 1
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
