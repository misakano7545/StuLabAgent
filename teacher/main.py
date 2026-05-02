"""Teacher entry: load config, start TCP server, tkinter main loop + event queue poll."""

from __future__ import annotations

import os
import sys
import json
import queue
import argparse
from typing import Any, Dict

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from common.paths import default_teacher_config_path, resolve_config_path
from common.protocol import (
    MSG_COMMAND_NETWORK_RESTRICT,
    MSG_COMMAND_POWER,
    MSG_COMMAND_QUERY_IPV4_DETAIL,
    MSG_COMMAND_RENAME_HOST,
    MSG_COMMAND_SET_IPV4,
)

from teacher.server import TeacherServer
from teacher.ui import TeacherApp


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    p = argparse.ArgumentParser(description="机房管理教师端")
    p.add_argument(
        "--config",
        default=default_teacher_config_path(),
        help="配置文件路径（相对路径则相对可执行文件所在目录）",
    )
    args = p.parse_args()
    cfg_path = resolve_config_path(args.config)
    cfg = load_config(cfg_path)

    listen_host = str(cfg.get("listen_host", "0.0.0.0"))
    listen_port = int(cfg.get("listen_port", 18765))
    token = str(cfg.get("token", ""))
    hb_timeout = float(cfg.get("heartbeat_timeout_sec", 35.0))
    
    yaml_file_path = str(cfg.get("yaml_file_path", "agent/configs/config.yaml"))
    yaml_file_path = resolve_config_path(yaml_file_path)

    event_q: "queue.Queue[tuple[str, Dict[str, Any]]]" = queue.Queue()
    server = TeacherServer(listen_host, listen_port, token, event_q, hb_timeout, yaml_file_path)
    server.start()

    def enqueue_rename(session_id: str, cmd_id: str, new_hostname: str) -> bool:
        return server.enqueue_command(
            session_id,
            {
                "type": MSG_COMMAND_RENAME_HOST,
                "cmd_id": cmd_id,
                "new_hostname": new_hostname,
            },
        )

    def enqueue_set_ipv4(session_id: str, cmd_id: str, payload: Dict[str, Any]) -> bool:
        body = {"type": MSG_COMMAND_SET_IPV4, "cmd_id": cmd_id}
        body.update(payload)
        return server.enqueue_command(session_id, body)

    def enqueue_query_ipv4_detail(session_id: str, cmd_id: str) -> bool:
        return server.enqueue_command(
            session_id,
            {
                "type": MSG_COMMAND_QUERY_IPV4_DETAIL,
                "cmd_id": cmd_id,
            },
        )

    def enqueue_power(session_id: str, cmd_id: str, action: str) -> bool:
        return server.enqueue_command(
            session_id,
            {
                "type": MSG_COMMAND_POWER,
                "cmd_id": cmd_id,
                "action": action,
            },
        )

    def enqueue_network_restrict(session_id: str, cmd_id: str, payload: Dict[str, Any]) -> bool:
        body = {"type": MSG_COMMAND_NETWORK_RESTRICT, "cmd_id": cmd_id}
        body.update(payload)
        return server.enqueue_command(session_id, body)

    app = TeacherApp(
        server,
        enqueue_rename,
        enqueue_set_ipv4,
        enqueue_query_ipv4_detail,
        enqueue_power,
        enqueue_network_restrict,
    )
    app.log_line(
        "教师端已启动 监听 %s:%s token=%s"
        % (listen_host, listen_port, "已启用" if token else "未启用")
    )
    if os.path.exists(yaml_file_path):
        app.log_line(f"配置文件同步已启用 yaml={yaml_file_path}")
    else:
        app.log_line(f"警告: yaml 配置文件不存在 {yaml_file_path}")

    def poll_queue() -> None:
        try:
            while True:
                kind, data = event_q.get_nowait()
                if kind == "client_registered":
                    hn = data.get("hostname") or "-"
                    app.log_line(
                        "上线: %s [%s] %s (%s)"
                        % (
                            hn,
                            (data.get("machine_id") or "")[:12] or "-",
                            data.get("ipv4"),
                            data.get("addr"),
                        )
                    )
                elif kind == "client_disconnected":
                    app.log_line(
                        "断开: pc_name=%s session=%s"
                        % (data.get("hostname") or "-", data.get("session_id"))
                    )
                elif kind == "command_result":
                    app.handle_ipv4_detail_query_result(data)
                    app.log_line(
                        "结果 pc_name=%s cmd_id=%s ok=%s %s"
                        % (
                            data.get("hostname") or "-",
                            data.get("cmd_id"),
                            data.get("ok"),
                            data.get("message"),
                        )
                    )
                elif kind == "client_error":
                    app.log_line(
                        "连接错误: pc_name=%s session=%s %s"
                        % (
                            data.get("hostname") or "-",
                            data.get("session_id") or "-",
                            data.get("message"),
                        )
                    )
                elif kind == "heartbeat":
                    pass
        except queue.Empty:
            pass
        server.tick_offline()
        app.refresh_sessions(server.get_sessions_snapshot())
        app.after(400, poll_queue)

    app.after(300, poll_queue)
    app.mainloop()
    server.stop()


if __name__ == "__main__":
    main()