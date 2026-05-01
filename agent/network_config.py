"""Resolve default-route interface and apply IPv4 via netsh (Win7+)."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from common.wincli_escape import netsh_interface_name_arg


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
            key = k.strip().lower()
            val = v.strip()
            # 同一记录里可能出现多行 IPAddress=（数组）；后者会覆盖前者导致 Win7 上漏配
            if key in item:
                if val:
                    prev_parts = [x.strip() for x in item[key].split(",") if x.strip()]
                    if val not in prev_parts:
                        item[key] = item[key] + "," + val
            else:
                item[key] = val
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


def _ipv4_gateway_from_wmi_default_gateway(raw: str) -> str:
    """DefaultIPGateway 可能同时含 IPv4 与 IPv6（如 Win7 fe80）；取第一个可用的 IPv4。"""
    for t in _wmic_multivalue_tokens(raw or ""):
        t = t.strip()
        if _is_dotted_ipv4(t) and not _is_ipv4_link_local(t):
            return t
    g = _clean_wmic_value(raw or "").strip()
    if _is_dotted_ipv4(g) and not _is_ipv4_link_local(g):
        return g
    return ""


def _wmic_multivalue_tokens(s: str) -> List[str]:
    """展开 WMIC 里逗号/花括号数组字段（如 IPAddress），保留全部原子值供匹配。"""
    s = (s or "").strip()
    if not s:
        return []
    inner = s[1:-1].strip() if s.startswith("{") and s.endswith("}") else s
    out: List[str] = []
    for part in re.split(r"\s*,\s*", inner):
        p = part.strip()
        if p.startswith('"') and p.endswith('"'):
            p = p[1:-1]
        if p:
            out.append(p)
    return out


def _wmic_netconnectionid_for_adapter_index(adapter_index: int) -> Tuple[Optional[str], str]:
    """Win32_NetworkAdapter.Index（与 NICConfiguration.Index 一致）→ NetConnectionID。"""
    try:
        p = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_NetworkAdapter",
                "where",
                "Index=%d" % adapter_index,
                "get",
                "NetConnectionID,NetEnabled",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
        )
    except OSError as e:
        return None, "wmic adapter Index=%s: %s" % (adapter_index, e)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "wmic adapter failed").strip()[:300]
    enabled_name: Optional[str] = None
    any_name: Optional[str] = None
    for row in _parse_wmic_list(p.stdout or ""):
        nid = row.get("netconnectionid", "").strip()
        if not nid:
            continue
        net = row.get("netenabled", "").strip().lower() in ("true", "1")
        if net:
            enabled_name = nid
            break
        if any_name is None:
            any_name = nid
    if enabled_name:
        return enabled_name, "adapter.Index=%d" % adapter_index
    if any_name:
        return any_name, "adapter.Index=%d (NetEnabled=false)" % adapter_index
    return None, "no NetConnectionID for adapter.Index=%d" % adapter_index


def _wmic_nic_name_for_index(idx: int) -> Tuple[Optional[str], str]:
    """
    将「路由表 / NICConfiguration 使用的 InterfaceIndex」解析为 netsh 用的连接名（NetConnectionID）。

    Win7 上 Win32_NetworkAdapter.InterfaceIndex 常与路由表 ifIndex 不一致，故在直接匹配失败后，
    用 Win32_NetworkAdapterConfiguration.InterfaceIndex → Index → Win32_NetworkAdapter。
    """
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
            return nid, "wmic NetworkAdapter.InterfaceIndex"

    try:
        pc = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_NetworkAdapterConfiguration",
                "where",
                "InterfaceIndex=%d" % idx,
                "get",
                "Index,IPEnabled",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
            timeout=30,
        )
    except OSError as e:
        return None, "NetConnectionID not found for ifIndex %s (direct); NICConfig: %s" % (idx, e)
    if pc.returncode != 0:
        return None, "NetConnectionID not found for ifIndex %s (direct); NICConfig query: %s" % (
            idx,
            (pc.stderr or pc.stdout or "").strip()[:300],
        )
    cfg_rows = list(_parse_wmic_list(pc.stdout or ""))
    adapter_indices: List[int] = []
    for row in cfg_rows:
        ie = row.get("ipenabled", "").strip().lower()
        if ie not in ("true", "1"):
            continue
        m = re.search(r"\d+", row.get("index", ""))
        if m:
            adapter_indices.append(int(m.group()))
    if not adapter_indices:
        for row in cfg_rows:
            m = re.search(r"\d+", row.get("index", ""))
            if m:
                adapter_indices.append(int(m.group()))
    seen = set()
    ordered: List[int] = []
    for i in adapter_indices:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    for ai in ordered:
        name, why = _wmic_netconnectionid_for_adapter_index(ai)
        if name:
            return name, "wmic NICConfig.InterfaceIndex=%d → %s" % (idx, why)
    return None, "NetConnectionID not found for ifIndex %s (direct + NICConfig.Index fallback)" % idx


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


def _windows_has_net_tcpip_ps_cmdlets() -> bool:
    """Get-NetRoute / Get-NetIPConfiguration 需 Win8+（6.2）或 Win10+ 且带 NetTCPIP；Win7 无此 cmdlet。"""
    if sys.platform != "win32":
        return False
    v = sys.getwindowsversion()
    if v.major > 6:
        return True
    if v.major == 6 and v.minor >= 2:
        return True
    return False


def _powershell_has_get_net_route_cmdlet() -> bool:
    """
    是否实际存在 Get-NetRoute（NetTCPIP 模块）。
    仅用 getwindowsversion 不可靠：兼容模式/部分环境会误报为 6.2+，Win7 仍会执行失败。
    """
    if sys.platform != "win32":
        return False
    ps = "if (Get-Command Get-NetRoute -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
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
            timeout=20,
        )
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


def _interface_from_wmi_ip4_route_table() -> Tuple[Optional[str], str]:
    """
    Win7+：WMI Win32_IP4RouteTable 取默认路由 (0.0.0.0/0.0.0.0) 的 InterfaceIndex，
    不依赖 Win32_NetworkAdapterConfiguration.DefaultIPGateway 或 IPAddress 字符串反查。
    """
    try:
        p = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_IP4RouteTable",
                "where",
                "Destination='0.0.0.0' and Mask='0.0.0.0'",
                "get",
                "InterfaceIndex,Metric1",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
            timeout=30,
        )
    except OSError as e:
        return None, "wmic Win32_IP4RouteTable unavailable: %s" % e
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "wmic Win32_IP4RouteTable failed").strip()[:500]
    candidates: List[Tuple[int, int]] = []  # (metric, interface_index)
    for row in _parse_wmic_list(p.stdout or ""):
        m = re.search(r"\d+", row.get("interfaceindex", ""))
        if not m:
            continue
        idx = int(m.group())
        met_s = row.get("metric1", "").strip()
        try:
            metric = int(met_s) if met_s.isdigit() else 9999
        except ValueError:
            metric = 9999
        candidates.append((metric, idx))
    if not candidates:
        return None, "wmi Win32_IP4RouteTable no default row"
    candidates.sort(key=lambda x: x[0])
    idx = candidates[0][1]
    name, msg = _wmic_nic_name_for_index(idx)
    if name:
        return name, "wmi Win32_IP4RouteTable (%s)" % msg
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
        tokens = [t.lower() for t in _wmic_multivalue_tokens(raw_ip)]
        if not tokens:
            tokens = [_clean_wmic_value(raw_ip).lower()]
        if target not in tokens:
            continue
        m = re.search(r"\d+", row.get("interfaceindex", ""))
        if not m:
            continue
        name, msg = _wmic_nic_name_for_index(int(m.group()))
        if name:
            return name, "route print + wmi (%s)" % msg
    return None, "no WMI row for interface IP %s" % interface_ip


def _parse_route_print_default_route_row(route_text: str) -> Optional[Tuple[int, str, str]]:
    """
    解析 route print -4 中默认 IPv4 路由行（0.0.0.0 0.0.0.0 …）。
    返回 (metric, gateway_ipv4, interface_ipv4)；无则 None。
    """
    lines = route_text.replace("\r\n", "\n").split("\n")
    best: Optional[Tuple[int, str, str]] = None  # (metric, gw, iface_ip)
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
        gw_ip = parts[2]
        iface_ip = parts[3]
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", iface_ip):
            continue
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", gw_ip):
            continue
        try:
            metric = int(parts[4])
        except ValueError:
            continue
        if best is None or metric < best[0]:
            best = (metric, gw_ip, iface_ip)
    return best


def _parse_route_print_default_interface_ip(route_text: str) -> Optional[str]:
    """解析 route print -4 中 0.0.0.0/0.0.0.0 默认路由行，取「接口」列 IPv4（中英界面均多为数字列）。"""
    row = _parse_route_print_default_route_row(route_text)
    return row[2] if row else None


def _parse_route_print_default_gateway(route_text: str) -> Optional[str]:
    """默认路由行中的 IPv4 网关。"""
    row = _parse_route_print_default_route_row(route_text)
    return row[1] if row else None


def _route_print_ipv4_default_gateway() -> str:
    try:
        p = subprocess.run(
            ["route", "print", "-4"],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if p.returncode != 0:
        return ""
    g = _parse_route_print_default_gateway(p.stdout or "")
    return g if g else ""


def _wmic_nic_configuration_row_for_ipv4(target_ipv4: str) -> Optional[Dict[str, str]]:
    """
    Win7 等环境下路由表 InterfaceIndex 与 NICConfiguration.InterfaceIndex 可能不一致；
    按本机 IPv4 在 WMI 全部已启用配置中反查对应行。
    """
    target = (target_ipv4 or "").strip().lower()
    if not target or not _is_dotted_ipv4(target):
        return None
    try:
        p = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_NetworkAdapterConfiguration",
                "where",
                "IPEnabled=true",
                "get",
                "InterfaceIndex,IPAddress,IPSubnet,DefaultIPGateway,DNSServerSearchOrder,DHCPEnabled,IPEnabled",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    for row in _parse_wmic_list(p.stdout or ""):
        if row.get("ipenabled", "").strip().lower() not in ("true", "1"):
            continue
        raw_ip = row.get("ipaddress", "")
        tokens = [t.strip().lower() for t in _wmic_multivalue_tokens(raw_ip)]
        if not tokens:
            t0 = _clean_wmic_value(raw_ip).strip().lower()
            if t0:
                tokens = [t0]
        if target in tokens:
            return row
    return None


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
    依次尝试：WMI DefaultIPGateway →（若存在 cmdlet）PowerShell 默认路由 / NetIPConfiguration
    → WMI Win32_IP4RouteTable → route print + WMI 反查。
    """
    if sys.platform != "win32":
        return None, "not windows"

    tried: List[str] = []

    name, why = _interface_from_wmi_default_gateway()
    tried.append("wmi:%s" % why)
    if name:
        return name, why

    if _powershell_has_get_net_route_cmdlet():
        name, why = _interface_from_powershell_net_route()
        tried.append("ps-route:%s" % why)
        if name:
            return name, why

        name, why = _interface_from_powershell_net_ip_configuration()
        tried.append("ps-ipcfg:%s" % why)
        if name:
            return name, why
    else:
        tried.append(
            "ps-route:skipped (no Get-NetRoute cmdlet; os claims net_tcpip=%s)"
            % _windows_has_net_tcpip_ps_cmdlets()
        )
        tried.append("ps-ipcfg:skipped (same)")

    name, why = _interface_from_wmi_ip4_route_table()
    tried.append("ip4route:%s" % why)
    if name:
        return name, why

    name, why = _interface_from_route_print_only()
    tried.append("route:%s" % why)
    if name:
        return name, why

    return None, "no default gateway adapter found; tried: %s" % (" | ".join(tried))[:900]


def apply_ipv4_dhcp(interface_name: str) -> Tuple[bool, str]:
    flags = _subprocess_flags()
    name_arg = netsh_interface_name_arg(interface_name)
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
    name_arg = netsh_interface_name_arg(interface_name)
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


def _outbound_ipv4_hint() -> str:
    """与 Agent 上报 IPv4 相同的启发式：连向公网时选用的本机源地址。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def _is_dotted_ipv4(s: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", (s or "").strip()))


def _is_ipv4_link_local(s: str) -> bool:
    return (s or "").strip().lower().startswith("169.254.")


def _wmic_interface_index_for_netconnection_id(target: str) -> Optional[int]:
    """NetConnectionID（netsh 接口名）→ Win32_NetworkAdapter.InterfaceIndex。"""
    want = (target or "").strip().lower()
    if not want:
        return None
    try:
        p = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_NetworkAdapter",
                "where",
                "NetEnabled=true",
                "get",
                "NetConnectionID,InterfaceIndex",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    for row in _parse_wmic_list(p.stdout or ""):
        nid = row.get("netconnectionid", "").strip()
        m = re.search(r"\d+", row.get("interfaceindex", ""))
        if not nid or not m:
            continue
        if nid.lower() == want:
            return int(m.group())
    return None


def _wmic_nic_configuration_by_interface_index(if_idx: int) -> Optional[Dict[str, str]]:
    """Win7 上部分环境需用 Index= 而非 InterfaceIndex=，两种都试。"""
    for where in ("InterfaceIndex=%d" % if_idx, "Index=%d" % if_idx):
        try:
            p = subprocess.run(
                [
                    "wmic",
                    "path",
                    "Win32_NetworkAdapterConfiguration",
                    "where",
                    where,
                    "get",
                    "IPAddress,IPSubnet,DefaultIPGateway,DNSServerSearchOrder,DHCPEnabled,IPEnabled",
                    "/format:list",
                ],
                capture_output=True,
                text=True,
                creationflags=_subprocess_flags(),
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode != 0:
            continue
        rows = _parse_wmic_list(p.stdout or "")
        for row in rows:
            if row.get("ipenabled", "").strip().lower() in ("true", "1"):
                return row
        if rows:
            return rows[0]
    return None


def _ipv4_subnet_pairs_from_wmi_row(row: Dict[str, str]) -> List[Tuple[str, str]]:
    """
    WMI 中 IPAddress / IPSubnet 按下标对齐，但可能夹杂 IPv6。
    对每条 IPv4 优先取同下标的点分掩码；否则按第 j 条 IPv4 对应 subs 中第 j 个点分掩码。
    """
    ips_t = _wmic_multivalue_tokens(row.get("ipaddress", ""))
    subs_t = _wmic_multivalue_tokens(row.get("ipsubnet", ""))
    dotted_masks = [s.strip() for s in subs_t if _is_dotted_ipv4(s.strip())]
    v4_idx: List[int] = []
    for i, ip in enumerate(ips_t):
        ip = ip.strip()
        if _is_dotted_ipv4(ip) and not _is_ipv4_link_local(ip):
            v4_idx.append(i)
    out: List[Tuple[str, str]] = []
    for j, i in enumerate(v4_idx):
        ip = ips_t[i].strip()
        sub = ""
        if i < len(subs_t):
            cand = subs_t[i].strip()
            if _is_dotted_ipv4(cand):
                sub = cand
        if not sub and j < len(dotted_masks):
            sub = dotted_masks[j]
        out.append((ip, sub))
    return out


def _wmic_default_route_interface_index() -> Optional[int]:
    """与 _interface_from_wmi_ip4_route_table 相同：默认 IPv4 路由所在 InterfaceIndex。"""
    try:
        p = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_IP4RouteTable",
                "where",
                "Destination='0.0.0.0' and Mask='0.0.0.0'",
                "get",
                "InterfaceIndex,Metric1",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            creationflags=_subprocess_flags(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    candidates: List[Tuple[int, int]] = []
    for row in _parse_wmic_list(p.stdout or ""):
        m = re.search(r"\d+", row.get("interfaceindex", ""))
        if not m:
            continue
        idx = int(m.group())
        met_s = row.get("metric1", "").strip()
        try:
            metric = int(met_s) if met_s.isdigit() else 9999
        except ValueError:
            metric = 9999
        candidates.append((metric, idx))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _detail_row_to_dict(
    row: Dict[str, str], preferred_ip: str
) -> Dict[str, Any]:
    pairs = _ipv4_subnet_pairs_from_wmi_row(row)
    pref = (preferred_ip or "").strip().lower()
    chosen_ip, chosen_mask = "", ""
    for ip, sub in pairs:
        if pref and ip.lower() == pref:
            chosen_ip, chosen_mask = ip, sub
            break
    if not chosen_ip and pairs:
        chosen_ip, chosen_mask = pairs[0]
    gw = _ipv4_gateway_from_wmi_default_gateway(row.get("defaultipgateway", ""))
    if not gw or gw.upper() == "NULL":
        gw = ""
    dns_tokens = _wmic_multivalue_tokens(row.get("dnsserversearchorder", ""))
    dns_v4: List[str] = []
    for t in dns_tokens:
        t = t.strip()
        if _is_dotted_ipv4(t):
            dns_v4.append(t)
    dhcp_raw = row.get("dhcpenabled", "").strip().lower()
    dhcp = dhcp_raw in ("true", "1")
    return {
        "ip": chosen_ip,
        "mask": chosen_mask,
        "gateway": gw,
        "dns_primary": dns_v4[0] if dns_v4 else "",
        "dns_secondary": dns_v4[1] if len(dns_v4) > 1 else "",
        "dhcp": dhcp,
    }


def _powershell_ipv4_detail_by_default_route() -> Optional[Dict[str, Any]]:
    """
    用默认路由 InterfaceIndex 取 IPv4 地址/前缀/网关/DNS，不依赖 NetConnectionID 与 InterfaceAlias 字符串一致。
    """
    if not _powershell_has_get_net_route_cmdlet():
        return None
    ps = (
        "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1; "
        "if (-not $r) { Write-Output '{}'; exit 0 }; "
        "$idx = $r.InterfaceIndex; "
        "$n = Get-NetIPConfiguration -InterfaceIndex $idx -ErrorAction SilentlyContinue; "
        "$ips = @(Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Where-Object { "
        "$_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } "
        "| Sort-Object SkipAsSource,InterfaceMetric); "
        "$pick = $ips | Select-Object -First 1; "
        "if (-not $pick) { Write-Output '{}'; exit 0 }; "
        "$gw = ''; "
        "if ($n -and $n.IPv4DefaultGateway -and $n.IPv4DefaultGateway.NextHop) "
        "{ $gw = ([string]$n.IPv4DefaultGateway.NextHop).Trim() }; "
        "$dns = @(Get-DnsClientServerAddress -InterfaceIndex $idx -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty ServerAddresses "
        "| Where-Object { $_ -match '^\\d+\\.\\d+\\.\\d+\\.\\d+$' }); "
        "if ($dns.Count -lt 1 -and $n -and $n.DNSServer) { "
        "foreach ($s in @($n.DNSServer)) { "
        "if ($s.AddressFamily -eq 'IPv4' -or $s.AddressFamily -eq 2) "
        "{ $dns = @($s.ServerAddresses | Where-Object { $_ -match '^\\d+\\.\\d+\\.\\d+\\.\\d+$' }); break } } }; "
        "$d1 = ''; $d2 = ''; "
        "if ($dns.Count -ge 1) { $d1 = [string]$dns[0] }; "
        "if ($dns.Count -ge 2) { $d2 = [string]$dns[1] }; "
        "$dhcp = ($pick.PrefixOrigin -eq 'Dhcp'); "
        "$pfx = [int]$pick.PrefixLength; "
        "$o = @{ ok=$true; ip=$pick.IPAddress; prefix=$pfx; gateway=$gw; "
        "dns_primary=$d1; dns_secondary=$d2; dhcp=$dhcp }; "
        "$o | ConvertTo-Json -Compress"
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
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if p.returncode != 0:
        return None
    raw = (p.stdout or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    prefix = data.get("prefix")
    mask = ""
    if prefix is not None:
        try:
            m = mask_from_prefix(str(int(prefix)))
            if m:
                mask = m
        except (TypeError, ValueError):
            pass
    return {
        "ip": str(data.get("ip") or "").strip(),
        "mask": mask,
        "gateway": str(data.get("gateway") or "").strip(),
        "dns_primary": str(data.get("dns_primary") or "").strip(),
        "dns_secondary": str(data.get("dns_secondary") or "").strip(),
        "dhcp": bool(data.get("dhcp")),
    }


def _merge_ipv4_detail_fields(base: Dict[str, Any], add: Dict[str, Any]) -> None:
    """把 add 中非空字段补入 base；dhcp 任一为真则标为 DHCP。"""
    for k in ("ip", "mask", "gateway", "dns_primary", "dns_secondary"):
        v = str(add.get(k) or "").strip()
        if v and not str(base.get(k) or "").strip():
            base[k] = v
    if "dhcp" in add:
        base["dhcp"] = bool(base.get("dhcp")) or bool(add.get("dhcp"))


_detail_mono = 0.0
_detail_payload: Dict[str, Any] = {}


def get_default_ipv4_detail_snapshot(ttl_sec: float = 20.0) -> Dict[str, Any]:
    """
    默认出口网卡的当前 IPv4 参数，供教师端静态表单预填。
    优先用 PowerShell（默认路由 InterfaceIndex，与网卡显示名无关），再用 WMI 补缺；带短时缓存。
    """
    global _detail_mono, _detail_payload
    now = time.monotonic()
    if _detail_payload and (now - _detail_mono) < ttl_sec:
        return dict(_detail_payload)

    if sys.platform != "win32":
        _detail_mono = now
        _detail_payload = {}
        return {}

    pref = _outbound_ipv4_hint().strip()

    out: Dict[str, Any] = {
        "ip": "",
        "mask": "",
        "gateway": "",
        "dns_primary": "",
        "dns_secondary": "",
        "dhcp": False,
    }

    psd = _powershell_ipv4_detail_by_default_route()
    if psd:
        out.update(psd)

    widx = _wmic_default_route_interface_index()
    if widx is not None:
        row = _wmic_nic_configuration_by_interface_index(widx)
        if row:
            _merge_ipv4_detail_fields(out, _detail_row_to_dict(row, pref))

    iface_name, _diag = get_default_ipv4_interface_name()
    if iface_name:
        if_idx = _wmic_interface_index_for_netconnection_id(iface_name)
        if if_idx is not None and (widx is None or if_idx != widx):
            row2 = _wmic_nic_configuration_by_interface_index(if_idx)
            if row2:
                _merge_ipv4_detail_fields(out, _detail_row_to_dict(row2, pref))

    if not str(out.get("ip") or "").strip() and pref:
        out["ip"] = pref

    hip = str(out.get("ip") or pref).strip()
    if hip:
        need_more = (
            not str(out.get("mask") or "").strip()
            or not str(out.get("gateway") or "").strip()
            or not str(out.get("dns_primary") or "").strip()
        )
        if need_more:
            row_fix = _wmic_nic_configuration_row_for_ipv4(hip)
            if row_fix:
                _merge_ipv4_detail_fields(out, _detail_row_to_dict(row_fix, hip))
    if not str(out.get("gateway") or "").strip():
        gw_rp = _route_print_ipv4_default_gateway()
        if gw_rp:
            out["gateway"] = gw_rp

    _detail_mono = now
    _detail_payload = dict(out)
    return dict(out)
