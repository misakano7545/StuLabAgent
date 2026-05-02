"""Common utility functions for the StuLabAgent project."""

from __future__ import annotations

import re
import sys
import subprocess
from typing import Dict, List


def is_admin() -> bool:
    """Check if current user has administrator privileges (Windows only)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def get_subprocess_flags() -> int:
    """Get subprocess creation flags for Windows (CREATE_NO_WINDOW) or 0 for other platforms."""
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def parse_wmic_list(text: str) -> List[Dict[str, str]]:
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


# Windows CLI quoting utilities
def wmic_wql_single_quote_body(s: str) -> str:
    """WQL string inside single quotes: double any apostrophe."""
    return s.replace("'", "''")


def wmic_where_eq(property_name: str, value: str) -> str:
    """WMIC argv token for where clause, e.g. name='MY-PC' (hyphen-safe)."""
    prop = property_name.strip()
    if not prop:
        raise ValueError("wmic_where_eq: empty property_name")
    return "%s='%s'" % (prop, wmic_wql_single_quote_body(value))


def wmic_call_arg_eq(arg_name: str, value: str) -> str:
    """WMIC method argv token after 'call rename', e.g. name='Win7-1' (hyphen-safe)."""
    name = arg_name.strip()
    if not name:
        raise ValueError("wmic_call_arg_eq: empty arg_name")
    return "%s='%s'" % (name, wmic_wql_single_quote_body(value))


def netsh_interface_name_arg(interface_name: str) -> str:
    """
    Single argv token for netsh ipv4 commands: name="...".
    Spaces, hyphens, and '&' in names are safe; embedded " doubled per Windows rules.
    """
    inner = (interface_name or "").replace('"', '""')
    return 'name="%s"' % inner