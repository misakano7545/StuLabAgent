"""Windows CLI quoting: WMIC / netsh parse tokens and treat '-' as switches unless quoted."""

from __future__ import annotations


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
