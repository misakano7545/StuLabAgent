"""Mihomo-compatible rule YAML builder - shared between teacher and agent."""

from __future__ import annotations

import socket
from typing import Any, Dict, List


def _is_ip(value: str) -> bool:
    try:
        socket.inet_aton(value)
        return True
    except socket.error:
        try:
            socket.inet_pton(socket.AF_INET6, value)
            return True
        except socket.error:
            return False


def _normalize_policy(mode: str, policy: str) -> str:
    p = (policy or "").strip().upper()
    if p:
        return p
    return "REJECT" if mode == "blacklist" else "DIRECT"


def _normalize_legacy_rule(mode: str, rule: Dict[str, str]) -> str:
    rule_type = str(rule.get("type", "")).strip().lower()
    value = str(rule.get("value", "")).strip()
    if not rule_type or not value:
        return ""

    policy = _normalize_policy(mode, "")
    if rule_type == "domain":
        return f"DOMAIN-SUFFIX,{value},{policy}"
    if rule_type == "ip":
        if _is_ip(value):
            return f"IP-CIDR,{value}/32,{policy}"
        return f"DOMAIN,{value},{policy}"
    if rule_type == "subnet":
        return f"IP-CIDR,{value},{policy}"
    return ""


def _normalize_mihomo_rule(mode: str, rule: Dict[str, str]) -> str:
    rule_type = str(rule.get("type", "")).strip().upper()
    payload = str(rule.get("payload", "")).strip()
    if not rule_type:
        return _normalize_legacy_rule(mode, rule)

    no_payload_types = {"MATCH"}
    if rule_type not in no_payload_types and not payload:
        return _normalize_legacy_rule(mode, rule)

    policy = _normalize_policy(mode, str(rule.get("policy", "")))
    extra = str(rule.get("extra", "")).strip()
    parts = [rule_type]
    if rule_type not in no_payload_types:
        parts.append(payload)
    parts.append(policy)
    if extra:
        parts.append(extra)
    return ",".join(parts)


def build_clash_config(mode: str, rules: List[Dict[str, str]]) -> Dict[str, Any]:
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
            line = _normalize_mihomo_rule(mode, rule)
            if line:
                clash_rules.append(line)
        clash_rules.append("MATCH,DIRECT")

    elif mode == "whitelist":
        for rule in rules:
            line = _normalize_mihomo_rule(mode, rule)
            if line:
                clash_rules.append(line)
        clash_rules.append("MATCH,REJECT")

    elif mode == "block_all":
        clash_rules.append("MATCH,REJECT")

    elif mode == "disable":
        clash_rules.append("MATCH,DIRECT")

    config["rules"] = clash_rules
    return config


def config_to_yaml(config: Dict[str, Any]) -> str:
    """Convert config dict to YAML string."""
    lines = []
    
    def add_line(key: str, value, indent: int = 0):
        prefix = "  " * indent
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            for k, v in value.items():
                add_line(k, v, indent + 1)
        elif isinstance(value, list):
            lines.append(f"{prefix}{key}:")
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"{prefix}-")
                    for k, v in item.items():
                        add_line(k, v, indent + 2)
                else:
                    lines.append(f"{prefix}- {item}")
        elif isinstance(value, bool):
            lines.append(f"{prefix}{key}: {str(value).lower()}")
        elif isinstance(value, int):
            lines.append(f"{prefix}{key}: {value}")
        else:
            lines.append(f"{prefix}{key}: {value}")
    
    for key, value in config.items():
        add_line(key, value)
    
    return "\n".join(lines)