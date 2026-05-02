"""machine_id from a single NIC MAC — 与计算机名、IP、ProgramData 文件无关。"""

from __future__ import annotations

import re
import sys
import json
import subprocess
from typing import List, Tuple
from common.utils import get_subprocess_flags, parse_wmic_list
from agent.network_config import get_default_ipv4_interface_name


def _normalize_mac(mac: str) -> str:
    if not mac:
        return ""
    hx = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(hx) != 12:
        return ""
    if hx.lower() == "0" * 12:
        return ""
    return hx.lower()


def _get_preferred_mac_normalized() -> str:
    """Return preferred NIC MAC in 12-hex lowercase, or empty string."""
    rows = _physical_adapter_mac_rows()
    if not rows:
        return ""

    default_name, _diag = get_default_ipv4_interface_name()
    if default_name:
        dn = default_name.strip().lower()
        for nid, raw in rows:
            if nid.strip().lower() == dn:
                n = _normalize_mac(raw)
                if n:
                    return n

    for _nid, raw in rows:
        n = _normalize_mac(raw)
        if n:
            return n
    return ""


def get_preferred_mac_display() -> str:
    """
    Return preferred NIC MAC in UI-friendly format: XX-XX-XX-XX-XX-XX.
    """
    n = _get_preferred_mac_normalized()
    if not n:
        return "MAC-UNKNOWN"
    return "-".join(n[i : i + 2] for i in range(0, 12, 2)).upper()


def _physical_adapter_mac_rows_wmi() -> List[Tuple[str, str]]:
    """(NetConnectionID or '', MACAddress raw) for 物理网卡；过滤 WMI 标记为未启用的条目。"""
    try:
        p = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_NetworkAdapter",
                "where",
                "PhysicalAdapter=TRUE",
                "get",
                "MACAddress,NetConnectionID,NetEnabled",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=get_subprocess_flags(),
        )
        if p.returncode != 0:
            return []
        out: List[Tuple[str, str]] = []
        for row in parse_wmic_list(p.stdout or ""):
            ne = row.get("netenabled", "").strip().lower()
            if ne in ("false", "0"):
                continue
            raw = row.get("macaddress", "").strip()
            if not raw or raw.upper() == "NULL":
                continue
            nid = row.get("netconnectionid", "").strip()
            out.append((nid, raw))
        return out
    except OSError:
        return []


def _physical_adapter_mac_rows_powershell() -> List[Tuple[str, str]]:
    """
    Win8+：与默认路由解析一致，用 Get-NetAdapter -Physical 取 MAC。
    多网卡时 WMIC 可能不可用、或 NetEnabled 与真实状态不一致导致 WMI 列为空；
    此处按 Status=Up 优先，便于与默认出口网卡对齐。
    """
    ps = (
        "$list = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue "
        "| Where-Object { $_.MacAddress }); "
        "if ($list.Count -eq 0) { Write-Output '[]'; exit 0 }; "
        "$up = @($list | Where-Object { $_.Status -eq 'Up' }); "
        "$ordered = if ($up.Count -gt 0) { @($up) } else { @($list) }; "
        "$rows = @($ordered | ForEach-Object { "
        "[PSCustomObject]@{ Name = [string]$_.InterfaceAlias; Mac = [string]$_.MacAddress } }); "
        "$rows | ConvertTo-Json -Compress"
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
            timeout=45,
            creationflags=get_subprocess_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if p.returncode != 0:
        return []
    raw = (p.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: List[Tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name", "")).strip()
        mac = str(item.get("Mac", "")).strip()
        if mac:
            out.append((name, mac))
    return out


def _physical_adapter_mac_rows() -> List[Tuple[str, str]]:
    """物理网卡 (连接显示名, MAC)；优先 PowerShell(Status=Up 排序)，再回退 WMI。"""
    rows = _physical_adapter_mac_rows_powershell()
    if rows:
        return rows
    return _physical_adapter_mac_rows_wmi()


def get_machine_id() -> str:
    """
    本机标识：单个物理网卡 MAC（小写连续 12 位十六进制），与 Windows 计算机名无关。

    优先取「当前默认 IPv4 出口网卡」对应的 MAC；
    若无法与默认出口名称匹配，则回退到“第一个可用的已启用物理网卡 MAC”。

    注意：部分无线网卡在 Win10+ 可能启用 **随机 MAC**，会导致标识变化；机房有线网卡一般稳定。
    """
    if sys.platform != "win32":
        return "nonwin"

    mac = _get_preferred_mac_normalized()
    return mac or "mac-unknown"