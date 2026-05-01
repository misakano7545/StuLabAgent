"""TCP server: agents connect, register, heartbeat; teacher queues commands per session."""

from __future__ import annotations

import collections
import errno
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from queue import Queue
from typing import Any, Callable, Deque, Dict, List, Optional

from common.protocol import (
    MSG_ACK,
    MSG_CONFIG_RESPONSE,
    MSG_HEARTBEAT,
    MSG_REGISTER,
    MSG_REGISTER_FAIL,
    MSG_REGISTER_OK,
    MSG_REQUEST_CONFIG,
    MSG_RESULT,
    get_message_type,
    read_frame_from_socket,
    write_frame_to_socket,
)


def _socket_exc_likely_peer_hangup(exc: BaseException) -> bool:
    """True when the client side probably closed or reset TCP (e.g. after IPv4 change)."""
    if isinstance(exc, ConnectionError):
        return True
    if isinstance(exc, OSError):
        w = getattr(exc, "winerror", None)
        if w in (10038, 10053, 10054, 10057):
            return True
        errno = exc.errno
        if errno is not None:
            if errno in (
                errno.ECONNRESET,
                errno.EPIPE,
                errno.ENOTCONN,
                errno.ECONNABORTED,
            ):
                return True
    return False


def _coerce_ipv4_detail(raw: object) -> Dict[str, Any]:
    """Agent 上报的 ipv4_detail 归一化，避免异常类型进入会话状态。"""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for k in ("ip", "mask", "gateway", "dns_primary", "dns_secondary"):
        if k not in raw:
            continue
        out[k] = str(raw.get(k) or "").strip()
    if "dhcp" in raw:
        out["dhcp"] = bool(raw.get("dhcp"))
    return out


@dataclass
class ClientSession:
    session_id: str
    addr: str
    machine_id: str = ""
    hostname: str = ""
    reported_ipv4: str = ""
    ipv4_detail: Dict[str, Any] = field(default_factory=dict)
    os_version: str = ""
    agent_version: str = ""
    last_seen: float = 0.0
    online: bool = True
    pending_commands: Deque[Dict[str, Any]] = field(default_factory=collections.deque)
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    conn: Optional[socket.socket] = field(default=None, repr=False)


EventHandler = Callable[[str, Dict[str, Any]], None]


class TeacherServer:
    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        token: str,
        event_queue: "Queue[tuple[str, Dict[str, Any]]]",
        heartbeat_timeout_sec: float = 35.0,
        yaml_file_path: str = "",
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.token = token.strip()
        self.event_queue = event_queue
        self.heartbeat_timeout_sec = heartbeat_timeout_sec
        self._yaml_file_path = yaml_file_path
        self._sessions_lock = threading.Lock()
        self._sessions: Dict[str, ClientSession] = {}
        self._conn_to_session: Dict[socket.socket, str] = {}
        self._sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def get_sessions_snapshot(self) -> List[ClientSession]:
        with self._sessions_lock:
            return list(self._sessions.values())

    def enqueue_command(self, session_id: str, command: Dict[str, Any]) -> bool:
        with self._sessions_lock:
            s = self._sessions.get(session_id)
            if not s:
                return False
            s.pending_commands.append(command)
            return True

    def _emit(self, kind: str, payload: Dict[str, Any]) -> None:
        self.event_queue.put((kind, payload))

    def _disconnect_same_machine_id(self, machine_id: str, keep_conn: socket.socket) -> None:
        """
        同一 machine_id 再次注册时（如改 IP 后重连），关闭旧 TCP。
        使用 Agent 上报的 machine_id（基于网卡 MAC），避免「同传后多台机计算机名相同」时误踢其它电脑。
        """
        mid = machine_id.strip().lower()
        if not mid:
            return
        to_close: List[socket.socket] = []
        with self._sessions_lock:
            for s in self._sessions.values():
                if s.machine_id.strip().lower() != mid:
                    continue
                c = s.conn
                if c is not None and c is not keep_conn:
                    to_close.append(c)
        for c in to_close:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def start(self) -> None:
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.listen_host, self.listen_port))
        self._sock.listen(32)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                self._sock.settimeout(1.0)
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(
                target=self._client_loop,
                args=(conn, addr[0]),
                daemon=True,
            )
            t.start()

    def _client_loop(self, conn: socket.socket, peer_ip: str) -> None:
        session_id: Optional[str] = None
        try:
            conn.settimeout(120.0)
            first = read_frame_from_socket(conn)
            if get_message_type(first) != MSG_REGISTER:
                write_frame_to_socket(
                    conn,
                    {
                        "type": MSG_REGISTER_FAIL,
                        "message": "first message must be register",
                    },
                )
                return
            if not self._check_token(first):
                write_frame_to_socket(
                    conn,
                    {
                        "type": MSG_REGISTER_FAIL,
                        "message": "invalid token",
                    },
                )
                return
            session_id = str(uuid.uuid4())
            machine_id = str(first.get("machine_id") or "")
            hostname = str(first.get("hostname") or "")
            reported_ipv4 = str(first.get("ipv4") or "")
            ipv4_detail = _coerce_ipv4_detail(first.get("ipv4_detail"))
            os_version = str(first.get("os_version") or "")
            agent_version = str(first.get("agent_version") or "")

            session = ClientSession(
                session_id=session_id,
                addr=peer_ip,
                machine_id=machine_id,
                hostname=hostname,
                reported_ipv4=reported_ipv4,
                ipv4_detail=ipv4_detail,
                os_version=os_version,
                agent_version=agent_version,
                last_seen=time.time(),
                conn=conn,
            )
            self._disconnect_same_machine_id(machine_id, conn)
            with self._sessions_lock:
                self._sessions[session_id] = session
                self._conn_to_session[conn] = session_id

            write_frame_to_socket(
                conn,
                {
                    "type": MSG_REGISTER_OK,
                    "session_id": session_id,
                },
            )
            self._emit(
                "client_registered",
                {
                    "session_id": session_id,
                    "machine_id": machine_id,
                    "hostname": hostname,
                    "ipv4": reported_ipv4,
                    "addr": peer_ip,
                    "os_version": os_version,
                    "agent_version": agent_version,
                },
            )

            while not self._stop.is_set():
                msg = read_frame_from_socket(conn)
                mtype = get_message_type(msg)
                if mtype == MSG_HEARTBEAT:
                    cmds: List[Dict[str, Any]] = []
                    host_for_emit = ""
                    ipv4_for_emit = ""
                    with self._sessions_lock:
                        s = self._sessions.get(session_id)
                        if s:
                            s.last_seen = time.time()
                            s.hostname = str(msg.get("hostname") or s.hostname)
                            s.reported_ipv4 = str(msg.get("ipv4") or s.reported_ipv4)
                            det = msg.get("ipv4_detail")
                            if isinstance(det, dict):
                                s.ipv4_detail = _coerce_ipv4_detail(det)
                            s.online = True
                            host_for_emit = s.hostname
                            ipv4_for_emit = s.reported_ipv4
                            while s.pending_commands:
                                cmds.append(s.pending_commands.popleft())
                    self._emit(
                        "heartbeat",
                        {
                            "session_id": session_id,
                            "hostname": host_for_emit,
                            "ipv4": ipv4_for_emit,
                        },
                    )
                    with session.write_lock:
                        write_frame_to_socket(
                            conn,
                            {
                                "type": MSG_ACK,
                                "commands": cmds,
                            },
                        )
                elif mtype == MSG_RESULT:
                    res_host = ""
                    with self._sessions_lock:
                        s_res = self._sessions.get(session_id)
                        if s_res:
                            res_host = str(s_res.hostname or "")
                    self._emit(
                        "command_result",
                        {
                            "session_id": session_id,
                            "hostname": res_host,
                            "cmd_id": msg.get("cmd_id"),
                            "ok": bool(msg.get("ok")),
                            "message": str(msg.get("message") or ""),
                        },
                    )
                elif mtype == MSG_REQUEST_CONFIG:
                    config_content = ""
                    if self._yaml_file_path and os.path.exists(self._yaml_file_path):
                        try:
                            with open(self._yaml_file_path, "r", encoding="utf-8") as f:
                                config_content = f.read()
                        except Exception as e:
                            self._emit(
                                "client_error",
                                {
                                    "session_id": session_id,
                                    "hostname": str(session.hostname or ""),
                                    "message": f"read config file error: {e}",
                                },
                            )
                    with session.write_lock:
                        write_frame_to_socket(
                            conn,
                            {
                                "type": MSG_CONFIG_RESPONSE,
                                "content": config_content,
                            },
                        )
                else:
                    with session.write_lock:
                        write_frame_to_socket(
                            conn,
                            {
                                "type": MSG_ACK,
                                "commands": [],
                                "warning": "unknown message type",
                            },
                        )
        except (ConnectionError, OSError, ValueError, json.JSONDecodeError) as e:
            err_host = ""
            if session_id:
                with self._sessions_lock:
                    s_err = self._sessions.get(session_id)
                    if s_err:
                        err_host = str(s_err.hostname or "")
            if not _socket_exc_likely_peer_hangup(e):
                self._emit(
                    "client_error",
                    {
                        "session_id": session_id or "",
                        "hostname": err_host,
                        "message": str(e),
                    },
                )
        finally:
            if session_id:
                disc_host = ""
                with self._sessions_lock:
                    s_disc = self._sessions.pop(session_id, None)
                    self._conn_to_session.pop(conn, None)
                    if s_disc:
                        disc_host = str(s_disc.hostname or "")
                self._emit(
                    "client_disconnected",
                    {"session_id": session_id, "hostname": disc_host},
                )
            try:
                conn.close()
            except OSError:
                pass

    def _check_token(self, register_msg: Dict[str, Any]) -> bool:
        if not self.token:
            return True
        got = register_msg.get("token")
        return isinstance(got, str) and got == self.token

    def tick_offline(self) -> List[str]:
        """Mark sessions with stale heartbeat offline; return affected session_ids."""
        now = time.time()
        changed: List[str] = []
        with self._sessions_lock:
            for sid, s in self._sessions.items():
                was_online = s.online
                if now - s.last_seen > self.heartbeat_timeout_sec:
                    s.online = False
                if was_online != s.online and not s.online:
                    changed.append(sid)
        return changed
