"""Clash-based network access restriction for student agents."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_script_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CLASH_CONFIG_DIR = os.path.join(_script_dir, "configs")
CLASH_CONFIG_FILE = os.path.join(CLASH_CONFIG_DIR, "config.yaml")
CLASH_PROCESS_NAME = "mihomo"
CLASH_PROCESS_LOCK = threading.Lock()
_clash_process: Optional[subprocess.Popen] = None


def _subprocess_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _get_clash_binary_path() -> Tuple[bool, str]:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else script_dir

    clash_path = os.path.join(project_root, "mihomo.exe")
    if os.path.isfile(clash_path):
        return True, clash_path
    clash_path = os.path.join(exe_dir, "mihomo.exe")
    if os.path.isfile(clash_path):
        return True, clash_path
    clash_path = os.path.join(exe_dir, "clash.exe")
    if os.path.isfile(clash_path):
        return True, clash_path
    if shutil.which("mihomo"):
        return True, "mihomo"
    if shutil.which("clash"):
        return True, "clash"
    return False, "Clash binary not found"


def _ensure_config_dir() -> None:
    os.makedirs(CLASH_CONFIG_DIR, exist_ok=True)


def _build_clash_config(mode: str, rules: List[Dict[str, str]]) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "port": 0,
        "socks-port": 0,
        "mixed-port": 0,
        "allow-lan": False,
        "bind-address": "\"*\"",
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "geodata-mode": True,
        "geox-url": {
            "geoip": "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.dat",
            "geosite": "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geosite.dat",
            "mmdb": "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/country.mmdb",
        },
        "dns": {
            "enable": True,
            "listen": "0.0.0.0:0",
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": ["223.5.5.5", "119.29.29.29"],
            "fallback": ["https://1.1.1.1/dns-query", "https://dns.google/dns-query"],
        },
        "tun": {
            "enable": True,
            "stack": "system",
            "dns-hijack": ["any:53"],
            "auto-route": True,
            "auto-detect-interface": True,
        },
        "proxies": [
            {
                "name": "http",
                "type": "http",
                "server": "10.0.0.1",
                "port": 443,
            },
        ],
        "proxy-groups": [
            {
                "name": "MANUAL",
                "type": "select",
                "proxies": ["DIRECT", "REJECT"],
            },
        ],
        "rules": [],
    }

    clash_rules: List[str] = []

    if mode == "blacklist":
        for rule in rules:
            rule_type = rule.get("type", "").strip().lower()
            value = rule.get("value", "").strip()
            if not rule_type or not value:
                continue

            if rule_type == "domain":
                clash_rules.append(f"DOMAIN-SUFFIX,{value},REJECT")
            elif rule_type == "ip":
                if _is_ip(value):
                    clash_rules.append(f"IP-CIDR,{value}/32,REJECT")
                else:
                    clash_rules.append(f"DOMAIN,{value},REJECT")
            elif rule_type == "subnet":
                clash_rules.append(f"IP-CIDR,{value},REJECT")

        clash_rules.append("MATCH,DIRECT")

    elif mode == "whitelist":
        for rule in rules:
            rule_type = rule.get("type", "").strip().lower()
            value = rule.get("value", "").strip()
            if not rule_type or not value:
                continue

            if rule_type == "domain":
                clash_rules.append(f"DOMAIN-SUFFIX,{value},DIRECT")
            elif rule_type == "ip":
                if _is_ip(value):
                    clash_rules.append(f"IP-CIDR,{value}/32,DIRECT")
                else:
                    clash_rules.append(f"DOMAIN,{value},DIRECT")
            elif rule_type == "subnet":
                clash_rules.append(f"IP-CIDR,{value},DIRECT")

        clash_rules.append("MATCH,REJECT")

    elif mode == "block_all":
        clash_rules.append("MATCH,REJECT")

    elif mode == "disable":
        clash_rules.append("MATCH,DIRECT")

    config["rules"] = clash_rules
    return config


def _is_ip(s: str) -> bool:
    try:
        socket.inet_aton(s)
        return True
    except socket.error:
        return False


def _save_config(config: Dict[str, Any]) -> Tuple[bool, str]:
    _ensure_config_dir()
    try:
        with open(CLASH_CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml_content = _dict_to_yaml(config)
            f.write(yaml_content)
        return True, "config saved"
    except Exception as e:
        return False, str(e)


def _dict_to_yaml(data: Any, indent: int = 0) -> str:
    result = []
    spaces = "  " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                result.append(f"{spaces}{key}:")
                result.append(_dict_to_yaml(value, indent + 1))
            elif isinstance(value, str) and ("#" in value or "\n" in value or value == ""):
                result.append(f"{spaces}{key}: |")
                for line in value.split("\n"):
                    result.append(f"{spaces}  {line}")
            elif isinstance(value, bool):
                result.append(f"{spaces}{key}: {'true' if value else 'false'}")
            elif value is None:
                result.append(f"{spaces}{key}: null")
            elif isinstance(value, list):
                if len(value) == 0:
                    result.append(f"{spaces}{key}: []")
                else:
                    first = value[0]
                    if isinstance(first, (dict, list)):
                        result.append(f"{spaces}{key}:")
                        for item in value:
                            result.append(f"{spaces}  -")
                            result.append(_dict_to_yaml(item, indent + 2))
                    else:
                        items = ", ".join(str(v) for v in value)
                        result.append(f"{spaces}{key}: [{items}]")
            else:
                result.append(f"{spaces}{key}: {value}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                result.append(f"{spaces}-")
                result.append(_dict_to_yaml(item, indent + 1))
            else:
                result.append(f"{spaces}- {item}")
    else:
        result.append(f"{spaces}{data}")
    return "\n".join(result)


def _stop_clash_process() -> None:
    global _clash_process
    with CLASH_PROCESS_LOCK:
        if _clash_process is not None:
            try:
                _clash_process.terminate()
                _clash_process.wait(timeout=5)
            except Exception:
                try:
                    _clash_process.kill()
                except Exception:
                    pass
            _clash_process = None


def _start_clash_process(clash_path: str) -> Tuple[bool, str]:
    global _clash_process
    _stop_clash_process()

    try:
        _clash_process = subprocess.Popen(
            [clash_path, "-f", CLASH_CONFIG_FILE, "-d", CLASH_CONFIG_DIR],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_subprocess_flags(),
        )
        time.sleep(1.5)
        if _clash_process.poll() is not None:
            return False, "Clash process exited immediately"
        return True, "Clash started"
    except Exception as e:
        return False, f"Failed to start Clash: {e}"


def _is_clash_running() -> bool:
    global _clash_process
    if _clash_process is None:
        return False
    if _clash_process.poll() is not None:
        return False
    return True


def apply_blacklist(rules: List[Dict[str, str]]) -> Tuple[bool, str]:
    if not is_admin():
        return False, "需要管理员权限"

    found, clash_path = _get_clash_binary_path()
    if not found:
        return False, clash_path

    config = _build_clash_config("blacklist", rules)
    ok, msg = _save_config(config)
    if not ok:
        return False, f"保存配置失败: {msg}"

    ok, msg = _start_clash_process(clash_path)
    if not ok:
        return False, f"启动 Clash 失败: {msg}"

    return True, f"已添加 {len(rules)} 条黑名单规则"


def apply_whitelist(rules: List[Dict[str, str]]) -> Tuple[bool, str]:
    if not is_admin():
        return False, "需要管理员权限"

    found, clash_path = _get_clash_binary_path()
    if not found:
        return False, clash_path

    config = _build_clash_config("whitelist", rules)
    ok, msg = _save_config(config)
    if not ok:
        return False, f"保存配置失败: {msg}"

    ok, msg = _start_clash_process(clash_path)
    if not ok:
        return False, f"启动 Clash 失败: {msg}"

    return True, f"已启用白名单模式，允许 {len(rules)} 个目标"


def block_all_network() -> Tuple[bool, str]:
    if not is_admin():
        return False, "需要管理员权限"

    found, clash_path = _get_clash_binary_path()
    if not found:
        return False, clash_path

    config = _build_clash_config("block_all", [])
    ok, msg = _save_config(config)
    if not ok:
        return False, f"保存配置失败: {msg}"

    ok, msg = _start_clash_process(clash_path)
    if not ok:
        return False, f"启动 Clash 失败: {msg}"

    return True, "已启用完全断网模式"


def disable_restrictions() -> Tuple[bool, str]:
    if not is_admin():
        return False, "需要管理员权限"

    _stop_clash_process()
    if os.path.exists(CLASH_CONFIG_FILE):
        os.remove(CLASH_CONFIG_FILE)

    return True, "已解除网络限制"


def start_clash_if_config_exists() -> Tuple[bool, str]:
    if not os.path.exists(CLASH_CONFIG_FILE):
        return False, "配置文件不存在"

    if not is_admin():
        return False, "需要管理员权限"

    found, clash_path = _get_clash_binary_path()
    if not found:
        return False, clash_path

    if _is_clash_running():
        return True, "Clash 已在运行"

    ok, msg = _start_clash_process(clash_path)
    if not ok:
        return False, f"启动 Clash 失败: {msg}"

    return True, "Clash 已启动"


def get_current_restriction_status() -> Dict[str, Any]:
    status: Dict[str, Any] = {
        "has_restrictions": _is_clash_running(),
        "mode": "disabled",
        "rule_count": 0,
        "rules": [],
    }

    if not os.path.exists(CLASH_CONFIG_FILE):
        return status

    try:
        with open(CLASH_CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read()
            if "MATCH,REJECT" in content:
                if "DOMAIN-SUFFIX" in content or "DOMAIN," in content or "IP-CIDR" in content:
                    if "DOMAIN-SUFFIX" in content or "DOMAIN," in content:
                        status["mode"] = "blacklist"
                    else:
                        status["mode"] = "block_all"
                else:
                    status["mode"] = "block_all"
                status["has_restrictions"] = True
    except Exception:
        pass

    return status
