"""Student agent entrypoint: connect teacher, heartbeat, execute commands."""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

from PIL import Image, ImageDraw

from common.paths import default_agent_config_path, resolve_config_path
from common.protocol import (
    MSG_ACK,
    MSG_COMMAND_NETWORK_RESTRICT,
    MSG_COMMAND_POWER,
    MSG_COMMAND_QUERY_IPV4_DETAIL,
    MSG_COMMAND_RENAME_HOST,
    MSG_COMMAND_SET_IPV4,
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

from agent.machine_identity import get_machine_id, get_preferred_mac_display
from agent.network_config import (
    apply_ipv4_dhcp,
    apply_ipv4_static,
    get_default_ipv4_detail_snapshot,
    get_default_ipv4_interface_name,
    is_admin,
)
from agent.rename_host import rename_computer
from agent.firewall_manager import (
    apply_blacklist,
    apply_whitelist,
    block_all_network,
    disable_restrictions,
    start_clash_if_config_exists,
)

AGENT_VERSION = "1.0.0"


def _debug(msg: str) -> None:
    """Only emit diagnostics when CLADM_DEBUG is explicitly enabled."""
    if os.environ.get("CLADM_DEBUG", "").strip().lower() in ("1", "true", "yes"):
        sys.stderr.write("%s\n" % msg)


def _guess_report_ipv4() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def _get_os_version() -> str:
    """Return a compact OS version string for teacher-side display."""
    try:
        if sys.platform == "win32":
            rel = platform.release() or "Windows"
            ver = platform.version() or ""
            return ("Windows %s %s" % (rel, ver)).strip()
        return ("%s %s" % (platform.system(), platform.release())).strip()
    except Exception:
        return ""


def _build_tray_icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (30, 36, 45, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((8, 8, 55, 55), outline=(72, 201, 112, 255), width=4)
    draw.ellipse((44, 2, 62, 20), fill=(220, 53, 69, 255))
    return img


def _start_tray_icon(mac_display: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import pystray  # type: ignore[import-not-found]
    except ImportError:
        _debug("pystray not available; skip tray icon")
        return

    title = "LabAgent | MAC: %s" % (mac_display or "MAC-UNKNOWN")
    icon = pystray.Icon("LabAgent", _build_tray_icon_image(), title)
    threading.Thread(target=icon.run, daemon=True).start()


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class AgentClient:
    def __init__(self, host: str, port: int, token: str, heartbeat_sec: float) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.heartbeat_sec = heartbeat_sec
        self.machine_id = get_machine_id()
        self._config_dir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__)), "configs")
        self._yaml_file = os.path.join(self._config_dir, "config.yaml")

        self._conn: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()

    def _sync_config(self, content: str) -> bool:
        try:
            os.makedirs(self._config_dir, exist_ok=True)
            
            if os.path.exists(self._yaml_file):
                with open(self._yaml_file, "r", encoding="utf-8") as f:
                    existing = f.read()
                if existing == content:
                    _debug("config already up to date")
                    return False
            
            with open(self._yaml_file, "w", encoding="utf-8") as f:
                f.write(content)
            _debug(f"config updated from teacher (teacher config takes precedence): {len(content)} bytes")
            
            self._start_clash_if_needed()
            
            return True
        except Exception as e:
            _debug(f"sync config error: {e}")
            return False

    def _start_clash_if_needed(self) -> None:
        if os.path.exists(self._yaml_file):
            _debug("config file exists, starting mihomo...")
            ok, msg = start_clash_if_config_exists()
            if ok:
                _debug(f"mihomo started: {msg}")
            else:
                _debug(f"failed to start mihomo: {msg}")
        else:
            _debug("no config file, skipping mihomo startup")

    def _remove_config_if_exists(self) -> None:
        if os.path.exists(self._yaml_file):
            try:
                os.remove(self._yaml_file)
                _debug(f"removed local config file: {self._yaml_file}")
            except Exception as e:
                _debug(f"failed to remove config file: {e}")

    def _request_config(self) -> None:
        try:
            self._send({"type": MSG_REQUEST_CONFIG})
        except (ConnectionError, OSError, ValueError):
            self._stop.set()

    def _connect(self) -> socket.socket:
        conn = socket.create_connection((self.host, self.port), timeout=8.0)
        conn.settimeout(20.0)
        return conn

    def _send(self, payload: Dict[str, Any]) -> None:
        conn = self._conn
        if conn is None:
            raise ConnectionError("not connected")
        with self._send_lock:
            write_frame_to_socket(conn, payload)

    def _register(self, conn: socket.socket) -> bool:
        write_frame_to_socket(
            conn,
            {
                "type": MSG_REGISTER,
                "hostname": socket.gethostname(),
                "machine_id": self.machine_id,
                "ipv4": _guess_report_ipv4(),
                "ipv4_detail": get_default_ipv4_detail_snapshot(),
                "os_version": _get_os_version(),
                "agent_version": AGENT_VERSION,
                # Keep legacy key for backward compatibility with old teacher builds.
                "version": AGENT_VERSION,
                "token": self.token,
            },
        )
        resp = read_frame_from_socket(conn)
        t = get_message_type(resp)
        if t == MSG_REGISTER_OK:
            return True
        if t == MSG_REGISTER_FAIL:
            _debug("register failed: %s" % str(resp.get("message", "")))
        else:
            _debug("unexpected register response: %s" % str(resp))
        return False

    def _schedule_reboot(self) -> None:
        def _run() -> None:
            time.sleep(0.35)
            try:
                subprocess.run(
                    ["shutdown", "-r", "-t", "1"],
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32"
                    else 0,
                    timeout=15,
                )
            except OSError:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _schedule_power_action(self, action: str) -> None:
        arg = "-r" if action == "reboot" else "-s"

        def _run() -> None:
            time.sleep(0.35)
            try:
                subprocess.run(
                    ["shutdown", arg, "-t", "1"],
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32"
                    else 0,
                    timeout=15,
                )
            except OSError:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _try_presend_ipv4_result(self, cmd_id: str) -> bool:
        """Notify teacher before netsh; TCP often dies after IP change (WinError 10038)."""
        if not cmd_id:
            return False
        try:
            self._send(
                {
                    "type": MSG_RESULT,
                    "cmd_id": cmd_id,
                    "ok": True,
                    "message": (
                        "正在通过 netsh 应用 IPv4；成功后本会话通常会断开并重连。"
                        "若未再收到本条命令的第二条结果，一般以本机网络配置为准。"
                    ),
                }
            )
            return True
        except (ConnectionError, OSError, ValueError):
            return False

    def _handle_command(self, cmd: Dict[str, Any]) -> None:
        cmd_type = get_message_type(cmd)
        cmd_id = str(cmd.get("cmd_id") or "")
        ok = False
        msg = "unsupported command"
        skip_final_result = False

        if cmd_type == MSG_COMMAND_RENAME_HOST:
            new_name = str(cmd.get("new_hostname") or "").strip()
            ok, msg = rename_computer(new_name)
            if ok:
                msg = "%s; auto reboot in 1s" % msg
                self._schedule_reboot()
        elif cmd_type == MSG_COMMAND_QUERY_IPV4_DETAIL:
            snap = get_default_ipv4_detail_snapshot(ttl_sec=0.0)
            ok = True
            msg = "ipv4_detail"
            detail_payload = snap
            try:
                self._send(
                    {
                        "type": MSG_RESULT,
                        "cmd_id": cmd_id,
                        "ok": bool(ok),
                        "message": str(msg),
                        "ipv4_detail": detail_payload if isinstance(detail_payload, dict) else {},
                    }
                )
            except (ConnectionError, OSError, ValueError):
                self._stop.set()
            return
        elif cmd_type == MSG_COMMAND_SET_IPV4:
            name, diag = get_default_ipv4_interface_name()
            if not name:
                ok, msg = False, diag or "default interface not found"
            else:
                mode = str(cmd.get("mode") or "").strip().lower()
                if mode == "dhcp":
                    presend = (
                        sys.platform == "win32" and self._try_presend_ipv4_result(cmd_id)
                    )
                    ok, msg = apply_ipv4_dhcp(name)
                    if sys.platform == "win32" and ok and presend:
                        skip_final_result = True
                elif mode == "static":
                    ip = str(cmd.get("ip") or "").strip()
                    mask = str(cmd.get("mask") or "").strip()
                    gw = str(cmd.get("gateway") or "").strip() or None
                    dns1 = str(cmd.get("dns_primary") or "").strip() or None
                    dns2 = str(cmd.get("dns_secondary") or "").strip() or None
                    if not ip or not mask:
                        ok, msg = False, "missing ip/mask"
                    else:
                        presend = (
                            sys.platform == "win32"
                            and self._try_presend_ipv4_result(cmd_id)
                        )
                        ok, msg = apply_ipv4_static(name, ip, mask, gw, dns1, dns2)
                        if sys.platform == "win32" and ok and presend:
                            skip_final_result = True
                else:
                    ok, msg = False, "unknown mode"
        elif cmd_type == MSG_COMMAND_POWER:
            action = str(cmd.get("action") or "").strip().lower()
            if action in ("shutdown", "reboot"):
                ok = True
                msg = "power action scheduled: %s" % action
                self._schedule_power_action(action)
            else:
                ok = False
                msg = "invalid power action"
        elif cmd_type == MSG_COMMAND_NETWORK_RESTRICT:
            mode = str(cmd.get("mode") or "").strip().lower()
            rules = cmd.get("rules", [])
            if not isinstance(rules, list):
                rules = []
            
            config_content = str(cmd.get("config", "") or "")
            saved_config_ok = False
            if config_content:
                try:
                    os.makedirs(self._config_dir, exist_ok=True)
                    with open(self._yaml_file, "w", encoding="utf-8") as f:
                        f.write(config_content)
                    _debug(f"config saved to {self._yaml_file}")
                    saved_config_ok = True
                except Exception as e:
                    _debug(f"save config error: {e}")
            
            if mode == "disable":
                ok, msg = disable_restrictions()
                # Ensure local override config is removed when restrictions are lifted.
                self._remove_config_if_exists()
            elif saved_config_ok:
                ok, msg = start_clash_if_config_exists()
                if ok:
                    msg = "已应用教师端下发的 mihomo 配置"
            elif mode == "blacklist":
                ok, msg = apply_blacklist(rules)
            elif mode == "whitelist":
                ok, msg = apply_whitelist(rules)
            elif mode == "block_all":
                ok, msg = block_all_network()
            else:
                ok = False
                msg = "unknown mode: %s" % mode

        if skip_final_result:
            return

        try:
            self._send(
                {
                    "type": MSG_RESULT,
                    "cmd_id": cmd_id,
                    "ok": bool(ok),
                    "message": str(msg),
                }
            )
        except (ConnectionError, OSError, ValueError):
            self._stop.set()

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            conn = self._conn
            if conn is None:
                self._stop.set()
                break
            try:
                msg = read_frame_from_socket(conn)
            except (ConnectionError, OSError, ValueError):
                self._stop.set()
                break
            mtype = get_message_type(msg)
            if mtype == MSG_ACK:
                commands = msg.get("commands", [])
                if not isinstance(commands, list):
                    continue
                for cmd in commands:
                    if self._stop.is_set():
                        break
                    if isinstance(cmd, dict):
                        self._handle_command(cmd)
            elif mtype == MSG_CONFIG_RESPONSE:
                content = str(msg.get("content", ""))
                if content:
                    self._sync_config(content)
                else:
                    self._remove_config_if_exists()

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._send(
                    {
                        "type": MSG_HEARTBEAT,
                        "hostname": socket.gethostname(),
                        "ipv4": _guess_report_ipv4(),
                        "ipv4_detail": get_default_ipv4_detail_snapshot(),
                    }
                )
            except (ConnectionError, OSError, ValueError):
                self._stop.set()
                break
            if self._stop.wait(self.heartbeat_sec):
                break

    def run_session(self) -> bool:
        conn = self._connect()
        self._conn = conn
        self._stop.clear()
        if not self._register(conn):
            try:
                conn.close()
            except OSError:
                pass
            self._conn = None
            return False

        self._request_config()

        rt = threading.Thread(target=self._reader_loop, daemon=True)
        ht = threading.Thread(target=self._heartbeat_loop, daemon=True)
        rt.start()
        ht.start()
        rt.join()
        self._stop.set()
        try:
            conn.close()
        except OSError:
            pass
        self._conn = None
        return True

    def run_forever(self) -> None:
        delay = 1.0
        while True:
            had_session = False
            try:
                had_session = self.run_session()
            except OSError as e:
                _debug("session error: %s" % str(e))

            if had_session:
                delay = 1.0
            jitter = random.uniform(0.0, min(1.2, delay * 0.2))
            time.sleep(delay + jitter)
            delay = min(delay * 1.8, 20.0)


def main() -> None:
    p = argparse.ArgumentParser(description="机房管理学生端 Agent")
    p.add_argument(
        "--config",
        default=default_agent_config_path(),
        help="配置文件路径（相对路径则相对可执行文件所在目录）",
    )
    args = p.parse_args()
    cfg = load_config(resolve_config_path(args.config))
    _start_tray_icon(get_preferred_mac_display())

    if not is_admin():
        if sys.stderr:
            sys.stderr.write(
                "warning: not running as administrator; rename/IP commands will fail\n"
            )

    host = str(cfg.get("teacher_host", "127.0.0.1"))
    port = int(cfg.get("teacher_port", 18765))
    token = str(cfg.get("token", ""))
    hb = float(cfg.get("heartbeat_interval_sec", 15.0))
    hb = max(1.0, hb)
    AgentClient(host, port, token, hb).run_forever()


if __name__ == "__main__":
    main()
