"""Rename Windows computer (Win7 / NT6.1 wmic, Win8+ PowerShell)."""

from __future__ import annotations

import os
import sys
import subprocess
from common.utils import wmic_call_arg_eq, wmic_where_eq, get_subprocess_flags


def rename_computer(new_name: str) -> tuple[bool, str]:
    """
    Rename local computer. Often requires reboot to fully apply.
    Returns (ok, message).
    """
    new_name = new_name.strip()
    if not new_name:
        return False, "empty name"
    if sys.platform != "win32":
        return False, "only supported on Windows"

    ver = sys.getwindowsversion()
    # NT 6.0–6.1: Vista / Win7 — use WMIC. Win8+ (6.2+) use PowerShell.
    use_wmic = (ver.major, ver.minor) < (6, 2)

    flags = get_subprocess_flags()

    try:
        if use_wmic:
            cur = os.environ.get("COMPUTERNAME", "")
            if not cur:
                return False, "COMPUTERNAME not set"
            where = wmic_where_eq("name", cur)
            name_arg = wmic_call_arg_eq("name", new_name)
            cmd = [
                "wmic",
                "computersystem",
                "where",
                where,
                "call",
                "rename",
                name_arg,
            ]
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=flags,
            )
            out = (p.stdout or "") + (p.stderr or "")
            if p.returncode != 0:
                return False, out.strip() or "wmic failed"
            if "ReturnValue = 0" in out:
                return True, "rename scheduled (reboot recommended)"
            return False, out.strip() or "wmic rename failed"
        ps = "Rename-Computer -NewName %r -Force -ErrorAction Stop" % new_name
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
            creationflags=flags,
        )
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            return False, out.strip() or "Rename-Computer failed"
        return True, "rename ok (reboot may be required)"
    except OSError as e:
        return False, str(e)
