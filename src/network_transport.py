"""C4P transport layer: 4-byte big-endian length-prefixed JSON framing.

Implements the framing rule from docs/protocol_blueprint.md:

    +--------------------+------------------------------+
    | 4-byte length (N)  | N bytes of UTF-8 JSON        |
    | struct "!I"        |                              |
    +--------------------+------------------------------+

TCP is a byte stream, so one send() does not equal one recv(). The blocking
helpers here (recv_exact / receive_c4p_message / send_c4p_message) are used
by client.py. The server uses its own incremental, non-blocking decoder but
shares encode_c4p_message() and MAX_FRAME_SIZE so both sides agree on the wire
format.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import struct
import time

logger = logging.getLogger("C4_Transport")

HEADER_SIZE = 4              # bytes in the length prefix
MAX_FRAME_SIZE = 65536       # 64 KiB payload cap (protocol blueprint 1.2)
PROTOCOL_VERSION = "1.0"
MIN_PORT = 1024
MAX_PORT = 65535


def port_number(value: str) -> int:
    """argparse type: accept an integer port in the range 1024-65535."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid port number (expected an integer)"
        )
    if not MIN_PORT <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(
            f"port {port} is out of range ({MIN_PORT}-{MAX_PORT})"
        )
    return port


def build_message(msg_type: str, player_id: str, payload: dict) -> dict:
    """Build a C4P envelope: msg_type, player_id, payload, timestamp."""
    return {
        "msg_type": msg_type,
        "player_id": player_id,
        "payload": payload,
        "timestamp": int(time.time()),
    }


def encode_c4p_message(message: dict) -> bytes:
    """Serialize a message dict to one complete frame (header + payload)."""
    payload = json.dumps(message).encode("utf-8")
    return struct.pack("!I", len(payload)) + payload


def recv_exact(sock: socket.socket, n_bytes: int) -> bytes | None:
    """Accumulate exactly n_bytes from a blocking TCP socket.

    A single recv() may return fewer bytes than requested (fragmentation), so
    we loop until the buffer is full. A 0-byte read means the peer closed the
    connection (TCP FIN / EOF); return None immediately so callers never spin.
    """
    buf = bytearray()
    while len(buf) < n_bytes:
        try:
            chunk = sock.recv(n_bytes - len(buf))
        except (ConnectionResetError, BrokenPipeError,
                ConnectionAbortedError, TimeoutError) as err:
            logger.warning("Connection dropped while reading: %s", err)
            return None
        if not chunk:
            logger.info("TCP EOF detected (remote socket closed).")
            return None
        buf.extend(chunk)
    return bytes(buf)


def receive_c4p_message(sock: socket.socket) -> dict | None:
    """Read one complete framed JSON message, or None on EOF / error."""
    header = recv_exact(sock, HEADER_SIZE)
    if header is None:
        return None
    (length,) = struct.unpack("!I", header)
    if length > MAX_FRAME_SIZE:
        logger.error("Frame length %d exceeds %d byte limit.",
                     length, MAX_FRAME_SIZE)
        return None
    body = recv_exact(sock, length)
    if body is None:
        return None
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.error("Received a frame that is not valid UTF-8 JSON.")
        return None
    return message if isinstance(message, dict) else None


def send_c4p_message(sock: socket.socket, message: dict) -> bool:
    """Frame and send a message on a blocking socket. True on success."""
    try:
        sock.sendall(encode_c4p_message(message))
        return True
    except (BrokenPipeError, ConnectionResetError,
            ConnectionAbortedError, OSError) as err:
        logger.error("Failed to transmit message: %s", err)
        return False
