"""Shared protocol and message types for teacher and agent."""

from common.protocol import (
    MSG_ACK,
    MSG_COMMAND_RENAME_HOST,
    MSG_COMMAND_SET_IPV4,
    MSG_ERROR,
    MSG_HEARTBEAT,
    MSG_REGISTER,
    MSG_REGISTER_FAIL,
    MSG_REGISTER_OK,
    MSG_RESULT,
    decode_frame,
    encode_frame,
    read_frame_from_socket,
    write_frame_to_socket,
)

__all__ = [
    "MSG_ACK",
    "MSG_COMMAND_RENAME_HOST",
    "MSG_COMMAND_SET_IPV4",
    "MSG_ERROR",
    "MSG_HEARTBEAT",
    "MSG_REGISTER",
    "MSG_REGISTER_FAIL",
    "MSG_REGISTER_OK",
    "MSG_RESULT",
    "decode_frame",
    "encode_frame",
    "read_frame_from_socket",
    "write_frame_to_socket",
]
