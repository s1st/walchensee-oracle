"""Shared real-browser traffic classification.

Single source of the bot/scanner filtering used by both
`scripts/dashboard_traffic.py` (offline gcloud analysis) and the dashboard's
statistics panel (live Cloud Logging query) — so the two can't drift.

Background notes worth keeping in mind when reading visitor counts:

- m-net (AS8767, prefix 2001:a61::/32) rotates the customer /48 periodically.
  The same person therefore shows up under multiple /64s over a month;
  do not double-count them as separate visitors.
- Cloud Run health probes and most scrapers send no User-Agent. Browsers
  always send a Mozilla/… UA. Anything else is treated as not-human.
- Exploit scanners (looking for /admin.php, /.git/config, /.aws/credentials,
  etc.) make up the bulk of the noise; they are dropped by path regex.
"""
from __future__ import annotations

import ipaddress
import re

BOT_RX = re.compile(
    r"bot|crawler|spider|HeadlessChrome|GoogleHC|kube-probe|UptimeRobot|"
    r"googlebot|bingbot|yandex|baidu|ahrefs|semrush|pingdom|datadog|monitis|"
    r"python-requests|curl|wget|Go-http-client|okhttp|libwww|Java/|httpclient",
    re.I,
)
EXPLOIT_RX = re.compile(
    r"\.(php|git|env|aspx?)|/admin|/wp-|/xmlrpc|/setup|/login\.|/owa|"
    r"/manager|/phpunit|credentials|parameters\.yml|settings\.py|"
    r"config\.json|config/application"
)

_HOST_RX = re.compile(r"^https?://[^/]+")


def normalize_ip(ip: str) -> str:
    """IPv6 → /64 prefix string; IPv4 → unchanged. /64 is the right grouping
    because IPv6 privacy addresses rotate the lower 64 bits within a stable
    customer prefix."""
    if not ip:
        return ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        net = ipaddress.IPv6Network((int(addr) & ((1 << 128) - (1 << 64)), 64))
        return f"{net.network_address}/64"
    return ip


def is_real_browser(ua: str) -> bool:
    return bool(ua) and ua.startswith("Mozilla/") and not BOT_RX.search(ua)


def is_mnet_prefix(ip_key: str) -> bool:
    """True if this /64 looks like an m-net customer prefix (2001:a61::/32).
    m-net rotates the /48 below this — so a single customer can appear under
    several /64s in the same month."""
    return ip_key.startswith("2001:a61:")


def real_browser_hit(ua: str, url: str, ip: str) -> tuple[str, str] | None:
    """Classify one request log entry.

    Returns (normalized_ip, path-without-query) for a real-browser hit,
    or None for bots, empty-UA probes and exploit-scanner paths.
    """
    if not is_real_browser(ua):
        return None
    path = _HOST_RX.sub("", url or "") or "/"
    if EXPLOIT_RX.search(path):
        return None
    return normalize_ip(ip or ""), path.split("?")[0]
