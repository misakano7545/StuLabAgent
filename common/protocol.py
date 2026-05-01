"""Length-prefixed JSON frames over TCP (big-endian 4-byte length + UTF-8 JSON)."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any, Dict, Optional, Tuple

# Message type strings (JSON field "type")
MSG_REGISTER = "register"
MSG_REGISTER_OK = "register_ok"
MSG_REGISTER_FAIL = "register_fail"
MSG_HEARTBEAT = "heartbeat"
MSG_ACK = "ack"
MSG_COMMAND_RENAME_HOST = "command_rename_host"
MSG_COMMAND_SET_IPV4 = "command_set_ipv4"
MSG_COMMAND_QUERY_IPV4_DETAIL = "command_query_ipv4_detail"
MSG_COMMAND_POWER = "command_power"
MSG_COMMAND_NETWORK_RESTRICT = "command_network_restrict"
MSG_RESULT = "result"
MSG_ERROR = "error"
MSG_REQUEST_CONFIG = "request_config"
MSG_CONFIG_RESPONSE = "config_response"


def encode_frame(obj: Dict[str, Any]) -> bytes:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 0xFFFFFF:
        raise ValueError("frame too large")
    return struct.pack(">I", len(body)) + body


def decode_frame(data: bytes) -> Tuple[Dict[str, Any], bytes]:
    """Decode one frame from buffer; return (dict, remaining_bytes)."""
    if len(data) < 4:
        raise ValueError("incomplete header")
    (n,) = struct.unpack(">I", data[:4])
    if len(data) < 4 + n:
        raise ValueError("incomplete body")
    body = data[4 : 4 + n]
    rest = data[4 + n :]
    obj = json.loads(body.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("frame must be a JSON object")
    return obj, rest


def recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf.extend(chunk)
    return bytes(buf)


def read_frame_from_socket(conn: socket.socket) -> Dict[str, Any]:
    header = recv_exact(conn, 4)
    (n,) = struct.unpack(">I", header)
    if n > 16 * 1024 * 1024:
        raise ValueError("frame size unreasonable")
    body = recv_exact(conn, n)
    obj = json.loads(body.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("frame must be a JSON object")
    return obj


def write_frame_to_socket(conn: socket.socket, obj: Dict[str, Any]) -> None:
    data = encode_frame(obj)
    conn.sendall(data)


def get_message_type(msg: Dict[str, Any]) -> Optional[str]:
    t = msg.get("type")
    return str(t) if isinstance(t, str) else None
