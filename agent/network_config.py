"""Resolve default-route interface and apply IPv4 via netsh (Win7+)."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple


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


def mask_from_prefix(prefix: str) -> Optional[str]:
    p = prefix.strip()
    if not p.isdigit():
        return None
    n = int(p)
    if n < 0 or n > 32:
        return None
    mask = (0xFFFFFFFF << (32 - n)) & 0xFFFFFFFF
    return ".".join(str((mask >> i) & 0xFF) for i in (24, 16, 8, 0))


def normalize_mask(mask: str) -> str:
    m = mask.strip()
    if m.isdigit() or (m.startswith("/") and m[1:].isdigit()):
        pref = m[1:] if m.startswith("/") else m
        dotted = mask_from_prefix(pref)
        return dotted if dotted else m
    return m


def _parse_wmic_list(text: str) -> List[Dict[str, str]]:
    """Parse WMIC /format:list output into dicts with lowercased keys."""
    text = text.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n").strip()
    # WMIC often uses blank line between records; also tolerate single-line runs
    blocks = re.split(r"\n\s*\n", text)
    rows: List[Dict[str, str]] = []
    for block in blocks:
        item: Dict[str, str] = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            item[k.strip().lower()] = v.strip()
        if item:
            rows.append(item)
    return rows


def _clean_wmic_value(s: str) -> str:
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if "," in inner:
            inner = inner.split(",")[0].strip()
        if inner.startswith('"') and inner.endswith('"'):
            inner = inner[1:-1]
        return inner
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


def _wmic_nic_name_for_index(idx: int) -> Tuple[Optional[str], str]:
    try:
        p2 = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_NetworkAdapter",
                "where",
                "NetEnabled=true",
                "get",
                "InterfaceIndex,NetConnectionID",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
        )
    except OSError as e:
        return None, "wmic unavailable: %s" % e
    if p2.returncode != 0:
        return None, (p2.stderr or p2.stdout or "wmic nic failed").strip()[:500]
    for row in _parse_wmic_list(p2.stdout or ""):
        m = re.search(r"\d+", row.get("interfaceindex", ""))
        if not m or int(m.group()) != idx:
            continue
        nid = row.get("netconnectionid", "").strip()
        if nid:
            return nid, "wmic"
    return None, "NetConnectionID not found for index %s" % idx


def _interface_from_wmi_default_gateway() -> Tuple[Optional[str], str]:
    """
    Prefer adapter whose WMI DefaultIPGateway is set (legacy logic).
    If multiple, pick lowest IPConnectionMetric when present.
    """
    try:
        p = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_NetworkAdapterConfiguration",
                "where",
                "IPEnabled=true",
                "get",
                "InterfaceIndex,DefaultIPGateway,IPAddress,IPConnectionMetric",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
        )
    except OSError as e:
        return None, "wmic unavailable: %s" % e
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "wmic failed").strip()[:500]
    candidates: List[Tuple[int, int]] = []  # (metric, interface_index)
    for row in _parse_wmic_list(p.stdout or ""):
        gw = _clean_wmic_value(row.get("defaultipgateway", ""))
        if not gw or gw.upper() == "NULL":
            continue
        m = re.search(r"\d+", row.get("interfaceindex", ""))
        if not m:
            continue
        idx = int(m.group())
        met_s = row.get("ipconnectionmetric", "").strip()
        try:
            metric = int(met_s) if met_s.isdigit() else 9999
        except ValueError:
            metric = 9999
        candidates.append((metric, idx))
    if not candidates:
        return None, "wmi no DefaultIPGateway"
    candidates.sort(key=lambda x: x[0])
    idx = candidates[0][1]
    name, msg = _wmic_nic_name_for_index(idx)
    if name:
        return name, "wmi DefaultIPGateway (%s)" % msg
    return None, msg


def _interface_from_powershell_net_route() -> Tuple[Optional[str], str]:
    """
    Win8+：用默认路由所在接口（不依赖 WMI 的 DefaultIPGateway 字段，避免静态/特殊驱动下为空）。
    """
    ps = (
        "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1; "
        "if (-not $r) { exit 2 }; "
        "(Get-NetIPInterface -InterfaceIndex $r.InterfaceIndex -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue).InterfaceAlias"
    )
    try:
        p = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return None, "powershell Get-NetRoute timeout"
    name = (p.stdout or "").strip()
    if p.returncode == 0 and name:
        return name, "powershell Get-NetRoute"
    err = ((p.stderr or "") + (p.stdout or "")).strip()[:300]
    return None, err or "powershell exit %s" % p.returncode


def _interface_from_powershell_net_ip_configuration() -> Tuple[Optional[str], str]:
    """Win8+：与旧版「有 IPv4 默认网关的适配器」等价，部分机器比 WMI 更可靠。"""
    ps = (
        "$c = Get-NetIPConfiguration -ErrorAction SilentlyContinue "
        "| Where-Object { $_.IPv4DefaultGateway -and $_.IPv4DefaultGateway.NextHop } "
        "| Sort-Object InterfaceMetric; "
        "if ($c) { $c[0].InterfaceAlias } else { '' }"
    )
    try:
        p = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return None, "powershell Get-NetIPConfiguration timeout"
    name = (p.stdout or "").strip()
    if p.returncode == 0 and name:
        return name, "powershell Get-NetIPConfiguration"
    return None, ((p.stderr or "") + (p.stdout or "")).strip()[:300]


def _interface_from_route_print(interface_ip: str) -> Tuple[Optional[str], str]:
    """根据 route print 得到的本机 IPv4，在 WMI 配置里反查 NetConnectionID。"""
    try:
        p = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_NetworkAdapterConfiguration",
                "where",
                "IPEnabled=true",
                "get",
                "InterfaceIndex,IPAddress",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
        )
    except OSError as e:
        return None, "wmic unavailable: %s" % e
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "wmic ip failed").strip()[:300]
    target = interface_ip.strip().lower()
    for row in _parse_wmic_list(p.stdout or ""):
        raw_ip = row.get("ipaddress", "")
        cleaned = _clean_wmic_value(raw_ip).lower()
        # 可能为 "a.b.c.d" 或逗号分隔多个
        parts = re.split(r"[\s,;]+", cleaned)
        if target not in parts and target not in cleaned.replace("{", "").replace("}", ""):
            continue
        m = re.search(r"\d+", row.get("interfaceindex", ""))
        if not m:
            continue
        name, msg = _wmic_nic_name_for_index(int(m.group()))
        if name:
            return name, "route print + wmi (%s)" % msg
    return None, "no WMI row for interface IP %s" % interface_ip


def _parse_route_print_default_interface_ip(route_text: str) -> Optional[str]:
    """解析 route print -4 中 0.0.0.0/0.0.0.0 默认路由行，取「接口」列 IPv4（中英界面均多为数字列）。"""
    lines = route_text.replace("\r\n", "\n").split("\n")
    best: Optional[Tuple[int, str]] = None  # (metric, interface_ip)
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped.startswith("0.0.0.0"):
            continue
        parts = line_stripped.split()
        # 0.0.0.0 0.0.0.0 <gateway> <interface_ip> <metric>
        if len(parts) < 5:
            continue
        if parts[1] != "0.0.0.0":
            continue
        iface_ip = parts[3]
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", iface_ip):
            continue
        try:
            metric = int(parts[4])
        except ValueError:
            continue
        if best is None or metric < best[0]:
            best = (metric, iface_ip)
    return best[1] if best else None


def _interface_from_route_print_only() -> Tuple[Optional[str], str]:
    """Win7+：route print -4 + WMI 反查（不依赖系统语言列标题）。"""
    p = subprocess.run(
        ["route", "print", "-4"],
        capture_output=True,
        text=True,
        creationflags=_subprocess_flags(),
        timeout=30,
    )
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "route print failed").strip()[:300]
    ip = _parse_route_print_default_interface_ip(p.stdout or "")
    if not ip:
        return None, "route print: no 0.0.0.0 default"
    return _interface_from_route_print(ip)


def get_default_ipv4_interface_name() -> Tuple[Optional[str], str]:
    """
    返回 (供 netsh 使用的接口名, 诊断说明)。
    依次尝试：WMI DefaultIPGateway → PowerShell 默认路由 → PowerShell NetIPConfiguration → route print。
    """
    if sys.platform != "win32":
        return None, "not windows"

    tried: List[str] = []

    name, why = _interface_from_wmi_default_gateway()
    tried.append("wmi:%s" % why)
    if name:
        return name, why

    name, why = _interface_from_powershell_net_route()
    tried.append("ps-route:%s" % why)
    if name:
        return name, why

    name, why = _interface_from_powershell_net_ip_configuration()
    tried.append("ps-ipcfg:%s" % why)
    if name:
        return name, why

    name, why = _interface_from_route_print_only()
    tried.append("route:%s" % why)
    if name:
        return name, why

    return None, "no default gateway adapter found; tried: %s" % (" | ".join(tried))[:900]


def apply_ipv4_dhcp(interface_name: str) -> Tuple[bool, str]:
    flags = _subprocess_flags()
    name_arg = "name=%s" % interface_name
    try:
        p1 = subprocess.run(
            ["netsh", "interface", "ipv4", "set", "address", name_arg, "dhcp"],
            capture_output=True,
            text=True,
            creationflags=flags,
        )
        o1 = (p1.stdout or "") + (p1.stderr or "")
        p2 = subprocess.run(
            ["netsh", "interface", "ipv4", "set", "dns", name_arg, "dhcp"],
            capture_output=True,
            text=True,
            creationflags=flags,
        )
        o2 = (p2.stdout or "") + (p2.stderr or "")
        if p1.returncode != 0:
            return False, o1.strip() or "netsh set address dhcp failed"
        if p2.returncode != 0:
            return False, o2.strip() or "netsh set dns dhcp failed"
        return True, "dhcp ok"
    except OSError as e:
        return False, str(e)


def apply_ipv4_static(
    interface_name: str,
    ip: str,
    mask: str,
    gateway: Optional[str],
    dns_primary: Optional[str],
    dns_secondary: Optional[str],
) -> Tuple[bool, str]:
    flags = _subprocess_flags()
    name_arg = "name=%s" % interface_name
    gw = gateway.strip() if gateway else ""
    gw_arg = gw if gw else "none"
    mask_n = normalize_mask(mask)
    try:
        p = subprocess.run(
            [
                "netsh",
                "interface",
                "ipv4",
                "set",
                "address",
                name_arg,
                "static",
                ip.strip(),
                mask_n,
                gw_arg,
            ],
            capture_output=True,
            text=True,
            creationflags=flags,
        )
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            return False, out.strip() or "netsh set address static failed"

        subprocess.run(
            ["netsh", "interface", "ipv4", "set", "dns", name_arg, "dhcp"],
            capture_output=True,
            text=True,
            creationflags=flags,
        )
        if dns_primary and dns_primary.strip():
            p2 = subprocess.run(
                [
                    "netsh",
                    "interface",
                    "ipv4",
                    "set",
                    "dns",
                    name_arg,
                    "static",
                    dns_primary.strip(),
                    "primary",
                ],
                capture_output=True,
                text=True,
                creationflags=flags,
            )
            o2 = (p2.stdout or "") + (p2.stderr or "")
            if p2.returncode != 0:
                return False, o2.strip() or "netsh set dns failed"
        if dns_secondary and dns_secondary.strip():
            p3 = subprocess.run(
                [
                    "netsh",
                    "interface",
                    "ipv4",
                    "add",
                    "dns",
                    name_arg,
                    dns_secondary.strip(),
                    "index=2",
                ],
                capture_output=True,
                text=True,
                creationflags=flags,
            )
            o3 = (p3.stdout or "") + (p3.stderr or "")
            if p3.returncode != 0:
                return False, o3.strip() or "netsh add dns failed"
        return True, "static ipv4 ok (connection may drop briefly)"
    except OSError as e:
        return False, str(e)
