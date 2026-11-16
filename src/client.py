#!/usr/bin/env python3
"""Connect 4 (C4P) client.

Usage:
    python3 client.py -h
    python3 client.py -i server.mata.edu -p 8080
    python3 client.py -i localhost -p 8080 -n Alice
"""

from __future__ import annotations

import argparse
import os
import re
import select
import socket
import sys

from game_engine import COLS, render_board
from network_transport import (PROTOCOL_VERSION, build_message, port_number,
                               receive_c4p_message, send_c4p_message)

ALIAS_RE = re.compile(r"[A-Za-z0-9]{1,16}")
CONNECT_TIMEOUT = 10.0
FATAL_ERRORS = {"ERR_LOBBY_FULL", "ERR_ALIAS_TAKEN"}


def alias_type(value: str) -> str:
    """argparse type: 1-16 alphanumeric characters."""
    if not ALIAS_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "nickname must be 1-16 letters or digits")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse client CLI arguments (-h, -i, -p)."""
    parser = argparse.ArgumentParser(
        description="Connect 4 (C4P) network client.",
        epilog="Example: python3 client.py -i server.mata.edu -p 8080")
    parser.add_argument("-i", "--host", required=True,
                        help="server DNS hostname (resolved via DNS)")
    parser.add_argument("-p", "--port", type=port_number, required=True,
                        help="server TCP port (1024-65535)")
    parser.add_argument("-n", "--name", type=alias_type,
                        help="your nickname (prompted if omitted)")
    return parser.parse_args(argv)


def resolve_host(hostname: str) -> str:
    """Resolve a hostname via DNS; exit cleanly with status 1 on failure."""
    try:
        return socket.gethostbyname(hostname)
    except (socket.gaierror, UnicodeError):
        print(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        print("Please check your Cisco router DNS configuration "
              "('ip dns server' / 'ip host') or verify domain spelling.")
        sys.exit(1)


def open_connection(ip: str, port: int, hostname: str) -> socket.socket:
    """Open a TCP connection, exiting cleanly with status 1 on failure."""
    try:
        sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
    except ConnectionRefusedError:
        print(f"[ERROR] Connection refused by {hostname} ({ip}:{port}). "
              "Is the server running on that port?")
        sys.exit(1)
    except OSError as err:
        print(f"[ERROR] Could not connect to {hostname} ({ip}:{port}): {err}")
        sys.exit(1)
    sock.settimeout(None)                  # blocking from here on
    return sock


def ask_alias() -> str:
    """Prompt for a valid nickname."""
    while True:
        try:
            name = input("Enter nickname (1-16 letters/digits): ").strip()
        except EOFError:
            sys.exit(1)
        if ALIAS_RE.fullmatch(name):
            return name
        print("Invalid nickname.")


def format_scores(scores: dict) -> str:
    """One-line score summary."""
    return " | ".join(f"{name}: {rec['wins']}W {rec['losses']}L "
                      f"{rec['draws']}D" for name, rec in scores.items())


class GameClient:
    """Interactive Connect 4 client session."""

    def __init__(self, sock: socket.socket, alias: str) -> None:
        self.sock = sock
        self.alias = alias
        self.role = "?"
        self.opponent = "opponent"
        self.my_turn = False
        self.over = False
        self.exit_code = 0
        self._stdin_buf = ""

    # -- input -----------------------------------------------------------
    def read_line(self) -> str | None:
        """Read a line from the keyboard.

        Returns None if a server message arrives first (so we never sit at a
        prompt while the opponent has already forfeited). Uses os.read() on
        stdin so no input is hidden in Python's buffer from select().
        """
        if os.name == "nt":                # select() cannot watch stdin
            try:
                return input()
            except EOFError:
                return "quit"
        fd = sys.stdin.fileno()
        while "\n" not in self._stdin_buf:
            readable, _, _ = select.select([fd, self.sock], [], [])
            if self.sock in readable:
                print()
                return None
            data = os.read(fd, 4096)
            if not data:
                return "quit"
            self._stdin_buf += data.decode("utf-8", errors="replace")
        line, self._stdin_buf = self._stdin_buf.split("\n", 1)
        return line.strip()

    def prompt_move(self) -> None:
        """Prompt until a local-valid column is sent, or input is aborted."""
        while self.my_turn and not self.over:
            print(f"\n>>> Your Turn ({self.role}) <<<")
            print(f"Choose a column 0-{COLS - 1} (or type 'quit'): ",
                  end="", flush=True)
            line = self.read_line()
            if line is None:
                return                     # server message pending
            if line.lower() in ("quit", "exit"):
                send_c4p_message(self.sock, build_message(
                    "DISCONNECT", self.alias, {"reason": "USER_QUIT"}))
                print("You left the game.")
                self.over = True
                return
            if not line.isdigit() or not 0 <= int(line) < COLS:
                print(f"Invalid input. Enter a number from 0 to {COLS - 1}.")
                continue
            if not send_c4p_message(self.sock, build_message(
                    "MOVE", self.alias, {"col": int(line)})):
                print("[ERROR] Lost connection to the server.")
                self.over, self.exit_code = True, 1
                return
            self.my_turn = False           # wait for the server's answer

    # -- server messages -------------------------------------------------
    def show_turn_status(self, current: str) -> None:
        """Update my_turn and print who is playing."""
        self.my_turn = current == self.alias
        if not self.my_turn:
            print(f"\nWaiting for {current}...")

    def handle(self, msg: dict) -> None:
        """React to one server message."""
        kind = msg.get("msg_type")
        body = msg.get("payload", {})
        if kind == "LOBBY_WAIT":
            print(body.get("message", "Waiting for opponent..."))
        elif kind == "GAME_START":
            self.role = body["your_role"]
            self.opponent = body["opponent_id"]
            print(f"\n=== Game start: you are {self.alias} ({self.role}) "
                  f"vs {self.opponent} ===")
            if body.get("scores"):
                print("Scores -", format_scores(body["scores"]))
            print(render_board(body["board"]))
            self.show_turn_status(body["starting_player"])
        elif kind == "STATE_UPDATE":
            move = body["last_move"]
            print(f"\n{move['player']} ({move['role']}) dropped a token in "
                  f"column {move['col']}.")
            print(render_board(body["board"]))
            self.show_turn_status(body["current_turn"])
        elif kind == "ERROR":
            print(f"\n[SERVER ERROR] {body.get('error_message')} "
                  f"({body.get('error_code')})")
            code = body.get("error_code")
            if code in FATAL_ERRORS:
                self.over, self.exit_code = True, 1
            elif code in ("ERR_INVALID_COLUMN", "ERR_COLUMN_FULL"):
                self.my_turn = True        # turn is not forfeited
        elif kind == "GAME_OVER":
            self.show_game_over(body)
        else:
            print(f"[WARN] Ignoring unknown message type: {kind}")

    def show_game_over(self, body: dict) -> None:
        """Print the final board, result, and scores."""
        self.over, self.my_turn = True, False
        print("\n=== GAME OVER ===")
        print(render_board(body["final_board"]))
        outcome, winner = body["outcome"], body.get("winner_id")
        if outcome == "DRAW":
            print("It's a draw!")
        elif outcome == "FORFEIT":
            print(f"{self.opponent} disconnected. You win by forfeit!")
        elif winner == self.alias:
            print("You WIN! Connect four!")
        else:
            print(f"You lose. {winner} connected four.")
        if body.get("scores"):
            print("Scores -", format_scores(body["scores"]))

    # -- main loop ---------------------------------------------------------
    def run(self) -> int:
        """Register with the server and play until the game ends."""
        if not send_c4p_message(self.sock, build_message(
                "CONNECT", self.alias,
                {"client_version": PROTOCOL_VERSION})):
            print("[ERROR] Could not send CONNECT to the server.")
            return 1
        while not self.over:
            if self.my_turn:
                self.prompt_move()
                if self.over:
                    break
            msg = receive_c4p_message(self.sock)
            if msg is None:
                print("\n[ERROR] Connection to the server was lost.")
                return 1
            self.handle(msg)
        return self.exit_code


def main() -> int:
    """Entry point."""
    args = parse_args()
    server_ip = resolve_host(args.host)
    alias = args.name or ask_alias()
    sock = open_connection(server_ip, args.port, args.host)
    print(f"Connected to {args.host} ({server_ip}:{args.port}).")
    try:
        return GameClient(sock, alias).run()
    except KeyboardInterrupt:
        send_c4p_message(sock, build_message(
            "DISCONNECT", alias, {"reason": "USER_QUIT"}))
        print("\nInterrupted. Left the game.")
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
